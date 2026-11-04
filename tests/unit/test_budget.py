"""Budget reservation SQL against a real migrated database."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update

from app.db import session_scope
from app.models import BudgetPeriod, Reservation, VirtualKey, utcnow
from app.models import Request as RequestRow
from app.services.budget import (
    commit_reservation,
    release_reservation,
    reserve,
    sweep_stale_reservations,
)


def _key_id() -> int:
    with session_scope() as session:
        return int(session.scalar(select(VirtualKey.id).where(VirtualKey.name == "test-key")))


def _request(key_id: int, request_id: str) -> str:
    with session_scope() as session:
        session.add(
            RequestRow(
                id=request_id,
                key_id=key_id,
                model="gpt-4.1-mini",
                status="in_flight",
                created_at=utcnow(),
            )
        )
    return request_id


def _reserve(
    key_id: int,
    request_id: str,
    estimate: str,
    limit: str = "1.00",
    ttl: timedelta = timedelta(minutes=15),
) -> int | None:
    with session_scope() as session:
        return reserve(
            session, request_id, key_id, Decimal(limit), Decimal(estimate), utcnow() + ttl
        )


def _period(key_id: int) -> BudgetPeriod:
    with session_scope() as session:
        return session.scalars(select(BudgetPeriod).where(BudgetPeriod.key_id == key_id)).one()


def test_reserve_creates_the_period_and_holds_the_estimate(db: Path, api_key: str) -> None:
    key_id = _key_id()
    reservation_id = _reserve(key_id, _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA1"), "0.40")

    assert reservation_id is not None
    period = _period(key_id)
    assert period.reserved_usd == Decimal("0.40")
    assert period.spent_usd == Decimal("0")
    with session_scope() as session:
        assert session.get(Reservation, reservation_id).state == "held"


def test_reserve_reuses_the_period_and_rejects_past_the_limit(db: Path, api_key: str) -> None:
    key_id = _key_id()
    first = _reserve(key_id, _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA1"), "0.60")
    second = _reserve(key_id, _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA2"), "0.40")
    third = _reserve(key_id, _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA3"), "0.01")

    assert first is not None and second is not None
    assert third is None
    with session_scope() as session:
        assert len(list(session.scalars(select(BudgetPeriod)))) == 1
    assert _period(key_id).reserved_usd == Decimal("1.00")


def test_commit_moves_the_hold_to_actual_spend(db: Path, api_key: str) -> None:
    key_id = _key_id()
    reservation_id = _reserve(key_id, _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA1"), "0.40")

    with session_scope() as session:
        assert commit_reservation(session, reservation_id, Decimal("0.25")) is True
    with session_scope() as session:
        assert commit_reservation(session, reservation_id, Decimal("0.25")) is False

    period = _period(key_id)
    assert period.reserved_usd == Decimal("0")
    assert period.spent_usd == Decimal("0.25")


def test_release_refunds_the_hold_once(db: Path, api_key: str) -> None:
    key_id = _key_id()
    reservation_id = _reserve(key_id, _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA1"), "0.40")

    with session_scope() as session:
        assert release_reservation(session, reservation_id) is True
    with session_scope() as session:
        assert release_reservation(session, reservation_id) is False

    period = _period(key_id)
    assert period.reserved_usd == Decimal("0")
    assert period.spent_usd == Decimal("0")


def test_sweep_fails_in_flight_requests_but_never_rewrites_finished_ones(
    db: Path, api_key: str
) -> None:
    key_id = _key_id()
    stale = timedelta(seconds=-1)
    stuck = _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA1")
    finished = _request(key_id, "01REQUESTAAAAAAAAAAAAAAAA2")
    _reserve(key_id, stuck, "0.30", ttl=stale)
    _reserve(key_id, finished, "0.30", ttl=stale)
    with session_scope() as session:
        session.execute(
            update(RequestRow)
            .where(RequestRow.id == finished)
            .values(status="ok", cost_usd=Decimal("0.10"))
        )

    sweep_stale_reservations()

    with session_scope() as session:
        stuck_row = session.get(RequestRow, stuck)
        finished_row = session.get(RequestRow, finished)
        states = set(session.scalars(select(Reservation.state)))
    assert (stuck_row.status, stuck_row.error_code) == ("failed", "swept")
    assert (finished_row.status, finished_row.error_code) == ("ok", None)
    assert states == {"released"}
    assert _period(key_id).reserved_usd == Decimal("0")
