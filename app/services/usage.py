"""Monthly usage reports and the spend reconciliation check behind `lgw usage --verify`."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import BudgetPeriod, VirtualKey
from app.models import Request as RequestRow

ZERO = Decimal("0")


@dataclass(frozen=True)
class KeyUsage:
    name: str
    requests: int
    cache_hits: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class Reconciliation:
    name: str
    spent_usd: Decimal
    requests_usd: Decimal
    reserved_usd: Decimal

    @property
    def ok(self) -> bool:
        # Costs are stored to 6 decimal places; compare at that precision.
        return abs(self.spent_usd - self.requests_usd) < Decimal("0.000001")


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """Return [start, end) for a YYYY-MM month. Raises ValueError for bad input."""
    start = datetime.strptime(month, "%Y-%m")
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def usage_report(session: Session, month: str, key_name: str | None = None) -> list[KeyUsage]:
    start, end = month_bounds(month)
    query = (
        select(
            VirtualKey.name,
            func.count(RequestRow.id),
            func.coalesce(func.sum(case((RequestRow.cache_hit, 1), else_=0)), 0),
            func.coalesce(func.sum(RequestRow.prompt_tokens), 0),
            func.coalesce(func.sum(RequestRow.completion_tokens), 0),
            func.coalesce(func.sum(RequestRow.cost_usd), 0),
        )
        .join(VirtualKey, VirtualKey.id == RequestRow.key_id)
        .where(RequestRow.created_at >= start, RequestRow.created_at < end)
        .group_by(VirtualKey.name)
        .order_by(VirtualKey.name)
    )
    if key_name:
        query = query.where(VirtualKey.name == key_name)
    return [
        KeyUsage(name, int(n), int(hits), int(prompt), int(completion), Decimal(str(cost)))
        for name, n, hits, prompt, completion, cost in session.execute(query)
    ]


def reconcile(session: Session, month: str) -> list[Reconciliation]:
    """Compare each budgeted key's committed spend with the cost of its requests that month."""
    start, end = month_bounds(month)
    periods = session.execute(
        select(VirtualKey.id, VirtualKey.name, BudgetPeriod.spent_usd, BudgetPeriod.reserved_usd)
        .join(VirtualKey, VirtualKey.id == BudgetPeriod.key_id)
        .where(BudgetPeriod.period == month)
        .order_by(VirtualKey.name)
    ).all()
    results = []
    for key_id, name, spent, reserved in periods:
        total = session.scalar(
            select(func.coalesce(func.sum(RequestRow.cost_usd), 0)).where(
                RequestRow.key_id == key_id,
                RequestRow.created_at >= start,
                RequestRow.created_at < end,
            )
        )
        results.append(
            Reconciliation(
                name,
                Decimal(str(spent or ZERO)),
                Decimal(str(total or ZERO)),
                Decimal(str(reserved or ZERO)),
            )
        )
    return results
