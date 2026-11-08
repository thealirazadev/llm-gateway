import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.auth import generate_key
from app.cli import main
from app.db import session_scope
from app.models import BudgetPeriod, VirtualKey, utcnow
from app.models import Request as RequestRow


def seed(spent: str = "0.300000") -> None:
    with session_scope() as session:
        key = VirtualKey(
            name="billing-app",
            key_hash=generate_key()[1],
            key_last4="abcd",
            cache_enabled=False,
            active=True,
            monthly_budget_usd=Decimal("50"),
            created_at=utcnow(),
        )
        session.add(key)
        session.flush()
        for n, (cost, hit, when) in enumerate(
            [
                ("0.100000", False, datetime(2026, 7, 3)),
                ("0.200000", True, datetime(2026, 7, 20)),
                ("9.000000", False, datetime(2026, 8, 1)),  # next month, excluded
            ]
        ):
            session.add(
                RequestRow(
                    id=f"01REQUEST{n:017d}",
                    key_id=key.id,
                    model="gpt-4.1-mini",
                    status="ok",
                    streamed=False,
                    cache_hit=hit,
                    prompt_tokens=100,
                    completion_tokens=50,
                    cost_usd=Decimal(cost),
                    created_at=when,
                )
            )
        session.add(
            BudgetPeriod(
                key_id=key.id,
                period="2026-07",
                spent_usd=Decimal(spent),
                reserved_usd=Decimal("0"),
            )
        )


def run(db: Path, *args: str) -> int:
    return main(["--db", f"sqlite:///{db}", "usage", "--month", "2026-07", *args])


def test_usage_table_totals_one_month(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed()

    assert run(db) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split() == ["billing-app", "2026-07", "2", "1", "200", "100", "0.3000"]
    assert lines[2].split() == ["TOTAL", "2", "1", "200", "100", "0.3000"]


def test_usage_json_uses_string_costs(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed()

    assert run(db, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_cost_usd"] == "0.3000"
    assert payload["keys"][0]["cache_hits"] == 1


def test_verify_passes_when_spend_matches(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed()

    assert run(db, "--verify") == 0
    assert capsys.readouterr().out.strip().endswith("ok")


def test_verify_fails_on_a_discrepancy(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(spent="0.250000")

    assert run(db, "--verify") == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_empty_month_and_bad_month(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", f"sqlite:///{db}", "usage", "--month", "2026-01"]) == 0
    assert "no usage recorded for 2026-01" in capsys.readouterr().out
    assert main(["--db", f"sqlite:///{db}", "usage", "--month", "July"]) == 2
