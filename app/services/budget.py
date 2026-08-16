"""Race-safe budget accounting with atomic reservation.

A monthly budget is split into spent (committed actuals) and reserved (in-flight estimates).
Reserve is one conditional UPDATE, atomic under SQLite's single-writer model. Commit, release,
and sweep all guard on state = 'held' so exactly one transition wins.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.db import session_scope
from app.errors import budget_exceeded
from app.logging import get_logger
from app.models import BudgetPeriod, Reservation, utcnow
from app.models import Request as RequestRow

logger = get_logger(__name__)


def _period_string(dt: datetime) -> str:
    """Month in YYYY-MM format for UTC date."""
    return dt.strftime("%Y-%m")


def reserve(
    session: Session,
    request_id: str,
    key_id: int,
    limit_usd: Decimal | None,
    estimate_usd: Decimal,
    expires_at: datetime,
) -> int | None:
    """Reserve an estimate against the key's current monthly budget. Return reservation id or None."""
    if limit_usd is None:
        return None

    now = utcnow()
    period = _period_string(now)

    # Create period row if it doesn't exist (idempotent).
    session.execute(
        insert(BudgetPeriod).values(
            key_id=key_id,
            period=period,
            spent_usd=Decimal("0"),
            reserved_usd=Decimal("0"),
        ).on_conflict_do_nothing()
    )

    # Get the period row.
    period_row = session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.key_id == key_id,
            BudgetPeriod.period == period,
        )
    )
    if not period_row:
        logger.error("budget.period_not_found", extra={"key_id": key_id, "period": period})
        return None

    # Atomically reserve: only one writer can win the conditional UPDATE.
    rows_affected = session.execute(
        update(BudgetPeriod)
        .where(
            BudgetPeriod.id == period_row.id,
            BudgetPeriod.spent_usd + BudgetPeriod.reserved_usd + estimate_usd <= limit_usd,
        )
        .values(reserved_usd=BudgetPeriod.reserved_usd + estimate_usd)
    ).rowcount

    if rows_affected == 0:
        return None

    # Record the held reservation.
    reservation = Reservation(
        request_id=request_id,
        period_id=period_row.id,
        amount_usd=estimate_usd,
        state="held",
        created_at=now,
        expires_at=expires_at,
    )
    session.add(reservation)
    session.flush()
    return reservation.id


def commit_reservation(
    session: Session,
    reservation_id: int,
    actual_cost_usd: Decimal,
) -> bool:
    """Transition a held reservation to committed and update spent. Return True if successful.

    Only the winner of the conditional UPDATE on state = 'held' succeeds; a race to commit
    against a sweep will result in one winner. If a sweep beats the commit, the commitment
    still increments spent; the reserved decrement was already done.
    """
    rows_affected = session.execute(
        update(Reservation)
        .where(
            Reservation.id == reservation_id,
            Reservation.state == "held",
        )
        .values(state="committed")
    ).rowcount

    if rows_affected == 0:
        return False

    # Get the reservation to find its period and refund amount.
    reservation = session.get(Reservation, reservation_id)
    if not reservation:
        return False

    # Update spent and release the reserved amount, then add actual cost.
    if reservation.period_id:
        session.execute(
            update(BudgetPeriod)
            .where(BudgetPeriod.id == reservation.period_id)
            .values(
                spent_usd=BudgetPeriod.spent_usd + actual_cost_usd,
                reserved_usd=BudgetPeriod.reserved_usd - reservation.amount_usd,
            )
        )

    return True


def release_reservation(
    session: Session,
    reservation_id: int,
) -> bool:
    """Transition a held reservation to released and refund the hold. Return True if successful."""
    rows_affected = session.execute(
        update(Reservation)
        .where(
            Reservation.id == reservation_id,
            Reservation.state == "held",
        )
        .values(state="released")
    ).rowcount

    if rows_affected == 0:
        return False

    # Get the reservation to find its period and amount.
    reservation = session.get(Reservation, reservation_id)
    if not reservation:
        return False

    # Refund the reserved amount.
    if reservation.period_id:
        session.execute(
            update(BudgetPeriod)
            .where(BudgetPeriod.id == reservation.period_id)
            .values(reserved_usd=BudgetPeriod.reserved_usd - reservation.amount_usd)
        )

    return True


def sweep_stale_reservations() -> None:
    """Release all held reservations past their expiration and mark their requests failed.

    This runs periodically to handle crashes between reserve and commit: if the process dies,
    the sweeper will eventually release the hold and the budget is freed without operator action.
    """
    now = utcnow()
    with session_scope() as session:
        # Find all stale reservations.
        stale = list(
            session.scalars(
                select(Reservation).where(
                    Reservation.state == "held",
                    Reservation.expires_at <= now,
                )
            )
        )

        for reservation in stale:
            # Transition to released.
            rows_affected = session.execute(
                update(Reservation)
                .where(
                    Reservation.id == reservation.id,
                    Reservation.state == "held",
                )
                .values(state="released")
            ).rowcount

            if rows_affected == 0:
                # Lost the race to commit or another sweep; skip this one.
                continue

            # Refund the reserved amount.
            if reservation.period_id:
                session.execute(
                    update(BudgetPeriod)
                    .where(BudgetPeriod.id == reservation.period_id)
                    .values(reserved_usd=BudgetPeriod.reserved_usd - reservation.amount_usd)
                )

            # Mark the request as failed with swept error code.
            session.execute(
                update(RequestRow)
                .where(RequestRow.id == reservation.request_id)
                .values(
                    status="failed",
                    error_code="swept",
                )
            )
            logger.info(
                "budget.reservation_swept",
                extra={
                    "request_id": reservation.request_id,
                    "amount_usd": str(reservation.amount_usd),
                },
            )
