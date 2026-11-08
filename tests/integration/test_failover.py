import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Attempt
from app.models import Request as RequestRow
from tests.conftest import openai_response

BODY = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def two_routes(seed_route, seed_price) -> None:
    seed_route(model="chat", provider_model="primary", position=1)
    seed_route(model="chat", provider_model="backup", position=2)
    seed_price(provider_model="primary", input_usd="100", output_usd="100")
    seed_price(provider_model="backup", input_usd="0.40", output_usd="1.60")


def primary_fails_with(failure):
    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "primary":
            if isinstance(failure, int):
                return httpx.Response(failure, json={"error": {"message": "upstream says no"}})
            raise failure(request)
        return httpx.Response(200, json=openai_response())

    return handler


def connect_timeout(request: httpx.Request) -> None:
    raise httpx.ConnectTimeout("timed out", request=request)


@pytest.mark.parametrize(
    "failure",
    [500, 429, 401, 403, connect_timeout],
    ids=["5xx", "429", "bad-provider-key", "forbidden", "timeout"],
)
async def test_retryable_failure_fails_over_and_bills_only_the_answering_route(
    client: httpx.AsyncClient, api_key: str, two_routes, upstream, failure
) -> None:
    upstream(primary_fails_with(failure))

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 200
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempts = list(session.scalars(select(Attempt).order_by(Attempt.position)))
    assert [a.provider_model for a in attempts] == ["primary", "backup"]
    assert [a.outcome == "ok" for a in attempts] == [False, True]
    assert row.provider_model == "backup"
    assert Decimal(row.cost_usd) == Decimal("0.000227")


async def test_client_error_does_not_fail_over(
    client: httpx.AsyncClient, api_key: str, two_routes, upstream
) -> None:
    upstream(primary_fails_with(400))

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "upstream_rejected"
    with session_scope() as session:
        attempts = list(session.scalars(select(Attempt)))
        row = session.scalar(select(RequestRow))
    assert [a.provider_model for a in attempts] == ["primary"]
    assert row.status == "failed"


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_code"),
    [(503, 502, "upstream_error"), (429, 429, "upstream_rate_limited")],
)
async def test_exhausted_routes_return_one_error(
    client: httpx.AsyncClient,
    api_key: str,
    two_routes,
    upstream,
    status_code: int,
    expected_status: int,
    expected_code: str,
) -> None:
    upstream(lambda request: httpx.Response(status_code, json={"error": {"message": "down"}}))

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    with session_scope() as session:
        assert len(list(session.scalars(select(Attempt)))) == 2


async def test_a_rejected_provider_key_is_never_relayed_to_the_caller(
    client: httpx.AsyncClient, api_key: str, two_routes, upstream
) -> None:
    leak = "Incorrect API key provided: sk-fake-****test."
    upstream(lambda request: httpx.Response(401, json={"error": {"message": leak}}))

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert "sk-" not in response.text
