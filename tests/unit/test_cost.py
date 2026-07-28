from datetime import datetime, timedelta
from decimal import Decimal

from app.db import session_scope
from app.models import Price
from app.services.cost import compute_cost, estimate_tokens, resolve_price

BASE = datetime(2026, 7, 1, 0, 0, 0)


def _price(input_usd: str, output_usd: str, effective_at: datetime) -> Price:
    return Price(
        provider="openai",
        provider_model="gpt-4.1-mini",
        input_usd_per_mtok=Decimal(input_usd),
        output_usd_per_mtok=Decimal(output_usd),
        effective_at=effective_at,
        created_at=BASE,
    )


def test_resolve_price_picks_the_newest_row_at_or_before_the_moment(db) -> None:
    with session_scope() as session:
        session.add(_price("0.15", "0.60", BASE))
        session.add(_price("0.40", "1.60", BASE + timedelta(days=10)))

    with session_scope() as session:
        assert resolve_price(session, "openai", "gpt-4.1-mini", BASE - timedelta(seconds=1)) is None
        on_boundary = resolve_price(session, "openai", "gpt-4.1-mini", BASE)
        between = resolve_price(session, "openai", "gpt-4.1-mini", BASE + timedelta(days=5))
        after = resolve_price(session, "openai", "gpt-4.1-mini", BASE + timedelta(days=20))

    assert Decimal(on_boundary.input_usd_per_mtok) == Decimal("0.15")
    assert Decimal(between.input_usd_per_mtok) == Decimal("0.15")
    assert Decimal(after.input_usd_per_mtok) == Decimal("0.40")


def test_resolve_price_is_scoped_to_provider_and_model(db) -> None:
    with session_scope() as session:
        session.add(_price("0.15", "0.60", BASE))

    with session_scope() as session:
        assert resolve_price(session, "anthropic", "gpt-4.1-mini", BASE) is None
        assert resolve_price(session, "openai", "other-model", BASE) is None


def test_compute_cost_uses_per_million_token_prices() -> None:
    price = _price("0.40", "1.60", BASE)
    assert compute_cost(price, 184, 96) == Decimal("0.000227")
    assert compute_cost(price, 1_000_000, 1_000_000) == Decimal("2.000000")
    assert compute_cost(price, 0, 0) == Decimal("0.000000")


def test_estimate_tokens_rounds_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("a" * 400) == 100
