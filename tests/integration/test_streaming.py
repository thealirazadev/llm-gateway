import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.logging import new_request_id
from app.models import Attempt, ModelRoute, VirtualKey, utcnow
from app.models import Request as RequestRow
from app.providers.openai import PROVIDER as OPENAI
from app.schemas import ChatCompletionRequest
from app.services.streaming import stream_completion
from tests.conftest import openai_chunk, openai_usage_chunk, sse_payloads, sse_upstream

BODY = {
    "model": "gpt-4.1-mini",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": True,
}

ANSWER = ["The ", "ticket ", "is ", "closed."]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def full_stream() -> tuple[str, ...]:
    return (
        openai_chunk({"role": "assistant", "content": ""}),
        *(openai_chunk({"content": piece}) for piece in ANSWER),
        openai_chunk({}, finish_reason="stop"),
        openai_usage_chunk(),
        "[DONE]",
    )


def content_of(payloads: list[str]) -> str:
    text = ""
    for payload in payloads:
        if payload == "[DONE]":
            continue
        for choice in json.loads(payload).get("choices", []):
            text += choice["delta"].get("content") or ""
    return text


async def test_streamed_answer_matches_the_provider_and_records_reported_usage(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    price_id = seed_price()
    upstream(sse_upstream(*full_stream()))

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-LGW-Provider"] == "openai"
    assert response.headers["X-LGW-Cache"] == "off"
    request_id = response.headers["X-LGW-Request-Id"]

    payloads = sse_payloads(response.text)
    assert payloads[-1] == "[DONE]"
    assert content_of(payloads) == "".join(ANSWER)

    first = json.loads(payloads[0])
    assert first["id"] == f"chatcmpl-{request_id}"
    assert first["object"] == "chat.completion.chunk"
    assert first["model"] == "gpt-4.1-mini"
    assert first["choices"][0]["delta"] == {"role": "assistant"}
    assert first["choices"][0]["finish_reason"] is None
    assert json.loads(payloads[-2])["choices"][0]["finish_reason"] == "stop"

    with session_scope() as session:
        row = session.get(RequestRow, request_id)
        attempts = list(session.scalars(select(Attempt)))
    assert row.status == "ok"
    assert row.streamed is True
    assert row.prompt_tokens == 184
    assert row.completion_tokens == 96
    assert row.tokens_estimated is False
    assert Decimal(row.cost_usd) == Decimal("0.000227")
    assert row.price_id == price_id
    assert row.ttfb_ms is not None
    assert row.latency_ms is not None
    assert len(attempts) == 1
    assert attempts[0].outcome == "ok"


async def test_usage_chunk_is_withheld_unless_the_client_asked_for_it(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    seed_price()
    upstream(sse_upstream(*full_stream()))

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    payloads = [p for p in sse_payloads(response.text) if p != "[DONE]"]
    assert all("usage" not in json.loads(payload) for payload in payloads)


async def test_include_usage_appends_a_final_usage_chunk(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    seed_price()
    upstream(sse_upstream(*full_stream()))

    body = dict(BODY, stream_options={"include_usage": True})
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)

    payloads = sse_payloads(response.text)
    final = json.loads(payloads[-2])
    assert final["choices"] == []
    assert final["usage"] == {
        "prompt_tokens": 184,
        "completion_tokens": 96,
        "total_tokens": 280,
    }


async def test_include_usage_is_always_requested_upstream(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seen: dict = {}
    inner = sse_upstream(*full_stream())

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return inner(request)

    seed_route()
    seed_price()
    upstream(handler)

    await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert seen["payload"]["stream"] is True
    assert seen["payload"]["stream_options"] == {"include_usage": True}


async def test_upstream_rejection_before_the_first_byte_is_a_json_error(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "upstream says no"}})

    seed_route()
    seed_price()
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "upstream_rejected"
    assert response.json()["error"]["message"] == "upstream says no"
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempt = session.scalar(select(Attempt))
    assert row.status == "failed"
    assert Decimal(row.cost_usd) == Decimal("0")
    assert attempt.outcome == "http_error"


async def test_upstream_death_mid_stream_ends_with_an_error_event_and_bills_the_chunks(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    seed_price()
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": ""}),
            openai_chunk({"content": "The "}),
            openai_chunk({"content": "ticket "}),
            json.dumps({"error": {"message": "upstream exploded", "type": "server_error"}}),
        )
    )

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 200
    payloads = sse_payloads(response.text)
    assert "[DONE]" not in payloads
    error = json.loads(payloads[-1])["error"]
    assert error["code"] == "upstream_error"
    assert error["type"] == "api_error"
    assert content_of(payloads[:-1]) == "The ticket "

    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempt = session.scalar(select(Attempt))
    assert row.status == "failed"
    assert row.error_code == "upstream_error"
    assert row.tokens_estimated is True
    assert row.completion_tokens == 3  # ceil(len("The ticket ") / 4)
    assert Decimal(row.cost_usd) > Decimal("0")
    assert attempt.outcome == "http_error"


async def test_a_broken_upstream_connection_mid_stream_is_billed_and_reported(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    async def dying_body():
        yield f"data: {openai_chunk({'role': 'assistant', 'content': ''})}\n\n".encode()
        yield f"data: {openai_chunk({'content': 'half'})}\n\n".encode()
        raise httpx.ReadError("connection lost")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=dying_body(), headers={"content-type": "text/event-stream"}
        )

    seed_route()
    seed_price()
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    payloads = sse_payloads(response.text)
    assert "[DONE]" not in payloads
    assert json.loads(payloads[-1])["error"]["code"] == "upstream_error"
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempt = session.scalar(select(Attempt))
    assert row.status == "failed"
    assert row.tokens_estimated is True
    assert attempt.outcome == "connect_error"


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_code", "expected_outcome"),
    [
        (500, 502, "upstream_error", "http_error"),
        (429, 429, "upstream_rate_limited", "rate_limited"),
    ],
)
async def test_stream_open_failures_map_to_documented_errors(
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
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "no"}})

    seed_route()
    seed_price()
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempt = session.scalar(select(Attempt))
    assert row.status == "failed"
    assert Decimal(row.cost_usd) == Decimal("0")
    assert attempt.outcome == expected_outcome


async def test_a_stream_that_times_out_before_the_first_byte_returns_502(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    seed_route()
    seed_price()
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 502
    with session_scope() as session:
        attempt = session.scalar(select(Attempt))
    assert attempt.outcome == "timeout"


async def test_a_placeholder_split_across_upstream_chunks_is_restored_in_the_relay(
    api_key: str, seed_route, seed_price, upstream
) -> None:
    """Phase 4 supplies the map; the relay is what has to survive the split."""
    seed_route()
    seed_price()
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": ""}),
            openai_chunk({"content": "Mail <pii_em"}),
            openai_chunk({"content": "ail_1> today"}),
            openai_chunk({}, finish_reason="stop"),
            openai_usage_chunk(),
            "[DONE]",
        )
    )

    request_id = new_request_id()
    with session_scope() as session:
        route = session.scalar(select(ModelRoute))
        key = session.scalar(select(VirtualKey))
        session.add(
            RequestRow(
                id=request_id,
                key_id=key.id,
                model="gpt-4.1-mini",
                provider=route.provider,
                provider_model=route.provider_model,
                status="in_flight",
                streamed=True,
                cache_hit=False,
                created_at=utcnow(),
            )
        )

    body = ChatCompletionRequest.model_validate(dict(BODY))
    response = await stream_completion(
        OPENAI, body, route, request_id, "off", {"<pii_email_1>": "jane@example.com"}
    )
    frames = [frame async for frame in response.body_iterator]
    await response.background()

    payloads = sse_payloads(b"".join(frames).decode())
    assert content_of(payloads) == "Mail jane@example.com today"

    with session_scope() as session:
        row = session.get(RequestRow, request_id)
    assert row.status == "ok"
    assert row.prompt_tokens == 184


async def test_a_stream_without_usage_falls_back_to_an_estimate(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    seed_price()
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": ""}),
            openai_chunk({"content": "Hello there."}),
            openai_chunk({}, finish_reason="stop"),
            "[DONE]",
        )
    )

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert sse_payloads(response.text)[-1] == "[DONE]"
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "ok"
    assert row.tokens_estimated is True
    assert row.completion_tokens == 3  # ceil(len("Hello there.") / 4)
