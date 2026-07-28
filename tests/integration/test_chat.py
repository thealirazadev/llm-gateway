import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Attempt, utcnow
from app.models import Request as RequestRow
from tests.conftest import openai_response

BODY = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "hi"}]}


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=openai_response())


def error_handler(status_code: int, payload: dict) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


async def test_successful_completion_returns_the_openai_shape(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    price_id = seed_price()
    upstream(ok_handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 200
    payload = response.json()
    request_id = response.headers["X-LGW-Request-Id"]
    assert payload["id"] == f"chatcmpl-{request_id}"
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "gpt-4.1-mini"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "Hello there."}
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"] == {
        "prompt_tokens": 184,
        "completion_tokens": 96,
        "total_tokens": 280,
    }
    assert response.headers["X-LGW-Provider"] == "openai"
    assert response.headers["X-LGW-Cache"] == "off"

    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempts = list(session.scalars(select(Attempt)))
    assert row.id == request_id
    assert row.status == "ok"
    assert row.provider == "openai"
    assert row.prompt_tokens == 184
    assert row.completion_tokens == 96
    assert Decimal(row.cost_usd) == Decimal("0.000227")
    assert row.price_id == price_id
    assert row.latency_ms is not None
    assert len(attempts) == 1
    assert attempts[0].outcome == "ok"
    assert attempts[0].status_code == 200


async def test_upstream_call_uses_the_route_provider_model_and_supported_fields(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json=openai_response())

    seed_route(provider_model="gpt-4.1-mini-upstream")
    seed_price(provider_model="gpt-4.1-mini-upstream")
    upstream(handler)

    body = dict(BODY, max_tokens=300, temperature=0.2)
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)

    assert response.status_code == 200
    assert seen["payload"]["model"] == "gpt-4.1-mini-upstream"
    assert seen["payload"]["max_tokens"] == 300
    assert seen["payload"]["temperature"] == 0.2
    assert "stop" not in seen["payload"]
    assert seen["auth"] == "Bearer sk-test-openai"


async def test_unknown_model_returns_404(client: httpx.AsyncClient, api_key: str, upstream) -> None:
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


@pytest.mark.parametrize(
    "body",
    [
        dict(BODY, tools=[{"type": "function"}]),
        dict(BODY, n=2),
        dict(BODY, frequency_penalty=1),
        {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": [{"type": "image"}]}]},
    ],
)
async def test_unsupported_parameters_are_rejected(
    client: httpx.AsyncClient, api_key: str, seed_route, body: dict
) -> None:
    seed_route()
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"


async def test_schema_violations_return_invalid_request(
    client: httpx.AsyncClient, api_key: str, seed_route
) -> None:
    seed_route()
    response = await client.post(
        "/v1/chat/completions", headers=auth(api_key), json={"model": "gpt-4.1-mini"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_malformed_json_returns_invalid_request(
    client: httpx.AsyncClient, api_key: str
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        headers={**auth(api_key), "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_oversize_body_returns_413(client: httpx.AsyncClient, api_key: str) -> None:
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "x" * 300_000}]}
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_code", "expected_outcome"),
    [
        (500, 502, "upstream_error", "http_error"),
        (429, 429, "upstream_rate_limited", "rate_limited"),
        (400, 400, "upstream_rejected", "http_error"),
    ],
)
async def test_upstream_failures_map_to_documented_errors(
    client: httpx.AsyncClient,
    api_key: str,
    seed_route,
    seed_price,
    upstream,
    status_code: int,
    expected_status: int,
    expected_code: str,
    expected_outcome: str,
) -> None:
    seed_route()
    seed_price()
    upstream(error_handler(status_code, {"error": {"message": "upstream says no"}}))

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempt = session.scalar(select(Attempt))
    assert row.status == "failed"
    assert row.error_code == expected_code
    assert Decimal(row.cost_usd) == Decimal("0")
    assert attempt.outcome == expected_outcome
    assert attempt.status_code == status_code


async def test_upstream_400_message_reaches_the_client(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    seed_price()
    upstream(error_handler(400, {"error": {"message": "upstream says no"}}))
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)
    assert response.json()["error"]["message"] == "upstream says no"


async def test_upstream_timeout_is_recorded_and_returns_502(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    seed_route()
    seed_price()
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    with session_scope() as session:
        attempt = session.scalar(select(Attempt))
    assert attempt.outcome == "timeout"


async def test_malformed_upstream_body_is_a_bad_response(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    seed_route()
    seed_price()
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 502
    with session_scope() as session:
        attempt = session.scalar(select(Attempt))
    assert attempt.outcome == "bad_response"


async def test_missing_price_row_still_serves_the_request_at_zero_cost(
    client: httpx.AsyncClient, api_key: str, seed_route, upstream
) -> None:
    seed_route()
    upstream(ok_handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 200
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "ok"
    assert row.price_id is None
    assert Decimal(row.cost_usd) == Decimal("0")


async def test_a_newer_price_row_costs_new_requests_only(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    old_price_id = seed_price(input_usd="0.15", output_usd="0.60")
    upstream(ok_handler)

    first = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)
    new_price_id = seed_price(
        input_usd="0.40", output_usd="1.60", effective_at=utcnow() - timedelta(seconds=1)
    )
    second = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    with session_scope() as session:
        first_row = session.get(RequestRow, first.headers["X-LGW-Request-Id"])
        second_row = session.get(RequestRow, second.headers["X-LGW-Request-Id"])

    assert first_row.price_id == old_price_id
    assert Decimal(first_row.cost_usd) == Decimal("0.000085")
    assert second_row.price_id == new_price_id
    assert Decimal(second_row.cost_usd) == Decimal("0.000227")
