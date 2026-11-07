import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.auth import generate_key
from app.db import session_scope
from app.models import BudgetPeriod, Reservation, VirtualKey, utcnow
from app.models import Request as RequestRow
from app.services.budget import reserve, sweep_stale_reservations
from tests.conftest import openai_chunk, openai_response, openai_usage_chunk, sse_upstream

# max_tokens 1000 at $1000 per million output tokens: every request holds exactly $1.00.
BODY = {
    "model": "gpt-4.1-mini",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 1000,
}


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def budget_key(db) -> object:
    def _create(limit_usd: str) -> str:
        raw_key, key_hash, last4 = generate_key()
        with session_scope() as session:
            session.add(
                VirtualKey(
                    name="budget-key",
                    key_hash=key_hash,
                    key_last4=last4,
                    cache_enabled=False,
                    active=True,
                    monthly_budget_usd=Decimal(limit_usd),
                    created_at=utcnow(),
                )
            )
        return raw_key

    return _create


@pytest.fixture
def priced_route(seed_route, seed_price) -> None:
    seed_route()
    seed_price(input_usd="0", output_usd="1000")


def period() -> BudgetPeriod:
    with session_scope() as session:
        row = session.scalar(select(BudgetPeriod))
        session.expunge_all()
        return row


def reservation_states() -> list[str]:
    with session_scope() as session:
        return [r.state for r in session.scalars(select(Reservation))]


async def test_request_over_budget_is_rejected_before_any_upstream_call(
    client: httpx.AsyncClient, budget_key, priced_route, upstream
) -> None:
    calls = []
    upstream(lambda request: calls.append(request) or httpx.Response(200, json=openai_response()))

    response = await client.post(
        "/v1/chat/completions", headers=auth(budget_key("0.50")), json=BODY
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "insufficient_quota"
    assert calls == []
    with session_scope() as session:
        assert session.scalar(select(RequestRow.status)) == "failed"
    assert reservation_states() == []


async def test_success_commits_actual_cost_and_frees_the_hold(
    client: httpx.AsyncClient, budget_key, priced_route, upstream
) -> None:
    upstream(lambda request: httpx.Response(200, json=openai_response(completion_tokens=96)))

    response = await client.post(
        "/v1/chat/completions", headers=auth(budget_key("10.00")), json=BODY
    )

    assert response.status_code == 200
    row = period()
    assert Decimal(row.spent_usd) == Decimal("0.096")
    assert Decimal(row.reserved_usd) == 0
    assert reservation_states() == ["committed"]


async def test_upstream_rejection_releases_the_hold(
    client: httpx.AsyncClient, budget_key, priced_route, upstream
) -> None:
    upstream(lambda request: httpx.Response(400, json={"error": {"message": "bad"}}))

    response = await client.post(
        "/v1/chat/completions", headers=auth(budget_key("10.00")), json=BODY
    )

    assert response.status_code == 400
    row = period()
    assert Decimal(row.spent_usd) == 0
    assert Decimal(row.reserved_usd) == 0
    assert reservation_states() == ["released"]


async def test_streamed_request_commits_its_cost_when_the_stream_ends(
    client: httpx.AsyncClient, budget_key, priced_route, upstream
) -> None:
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": "Hi"}),
            openai_chunk({}, finish_reason="stop"),
            openai_usage_chunk(completion_tokens=96),
            "[DONE]",
        )
    )

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth(budget_key("10.00")),
        json={**BODY, "stream": True},
    ) as response:
        assert response.status_code == 200
        await response.aread()

    row = period()
    assert Decimal(row.spent_usd) == Decimal("0.096")
    assert Decimal(row.reserved_usd) == 0
    assert reservation_states() == ["committed"]


async def test_concurrent_requests_admit_only_what_the_budget_covers(
    client: httpx.AsyncClient, budget_key, priced_route, upstream
) -> None:
    async def slow_ok(request: httpx.Request) -> httpx.Response:
        # Hold every admitted request upstream until all ten have tried to reserve.
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=openai_response(completion_tokens=96))

    upstream(slow_ok)
    key = budget_key("3.00")

    responses = await asyncio.gather(
        *(client.post("/v1/chat/completions", headers=auth(key), json=BODY) for _ in range(10))
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] * 3 + [429] * 7
    row = period()
    assert Decimal(row.spent_usd) == Decimal("0.288")
    assert Decimal(row.reserved_usd) == 0


async def test_sweeper_releases_a_hold_that_was_never_settled(budget_key, db) -> None:
    budget_key("5.00")
    with session_scope() as session:
        key = session.scalar(select(VirtualKey))
        session.add(
            RequestRow(
                id="01STALEREQUEST000000000000",
                key_id=key.id,
                model="gpt-4.1-mini",
                status="in_flight",
                streamed=False,
                cache_hit=False,
                created_at=utcnow(),
            )
        )
        session.flush()
        held = reserve(
            session,
            "01STALEREQUEST000000000000",
            key.id,
            key.monthly_budget_usd,
            Decimal("2.00"),
            expires_at=utcnow() - timedelta(seconds=1),
        )
    assert held is not None
    assert Decimal(period().reserved_usd) == Decimal("2.00")

    sweep_stale_reservations()

    assert Decimal(period().reserved_usd) == 0
    assert reservation_states() == ["released"]
    with session_scope() as session:
        request = session.scalar(select(RequestRow))
    assert (request.status, request.error_code) == ("failed", "swept")
