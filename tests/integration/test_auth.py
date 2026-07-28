import httpx
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Request as RequestRow
from app.models import VirtualKey, utcnow

BODY = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "hi"}]}


async def test_health_is_public_and_carries_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(response.headers["X-LGW-Request-Id"]) == 26


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "lgw_missing_scheme"}, {"Authorization": "Bearer "}],
)
async def test_missing_or_malformed_credentials_are_rejected(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_unknown_key_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer lgw_unknown"}, json=BODY
    )
    assert response.status_code == 401


async def test_revoked_key_is_rejected_on_the_next_request(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    with session_scope() as session:
        key = session.scalar(select(VirtualKey))
        key.active = False
        key.revoked_at = utcnow()

    response = await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=BODY
    )
    assert response.status_code == 401
    with session_scope() as session:
        assert session.scalar(select(RequestRow)) is None
