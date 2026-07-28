"""Cost from the versioned price table.

Prices are append-only: the row for a request is the newest one whose `effective_at` is at
or before the request time, so a later price change never re-costs history.
"""

import math
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Price, utcnow

MILLION = Decimal("1000000")
CENTS = Decimal("0.000001")


def resolve_price(
    session: Session,
    provider: str,
    provider_model: str,
    at: datetime | None = None,
) -> Price | None:
    moment = at or utcnow()
    return session.scalar(
        select(Price)
        .where(
            Price.provider == provider,
            Price.provider_model == provider_model,
            Price.effective_at <= moment,
        )
        .order_by(Price.effective_at.desc(), Price.id.desc())
        .limit(1)
    )


def compute_cost(price: Price, prompt_tokens: int, completion_tokens: int) -> Decimal:
    total = (Decimal(prompt_tokens) * Decimal(price.input_usd_per_mtok) / MILLION) + (
        Decimal(completion_tokens) * Decimal(price.output_usd_per_mtok) / MILLION
    )
    return total.quantize(CENTS, rounding=ROUND_HALF_UP)


def estimate_tokens(text: str) -> int:
    """Rough token count used only where provider usage is unavailable."""
    return math.ceil(len(text) / 4)
