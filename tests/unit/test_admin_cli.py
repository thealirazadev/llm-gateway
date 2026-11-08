"""The operator workflow from the README, run through the CLI entry point."""

from pathlib import Path

import pytest
from sqlalchemy import select

from app.auth import hash_key
from app.cli import main
from app.config import get_settings
from app.db import session_scope
from app.models import ModelRoute, VirtualKey


@pytest.fixture
def lgw(db: Path, capsys: pytest.CaptureFixture[str]):
    def run(*args: str) -> tuple[int, str]:
        code = main(["--db", f"sqlite:///{db}", *args])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return run


def test_a_created_key_is_shown_once_and_stored_only_as_a_hash(lgw) -> None:
    code, out = lgw("keys", "create", "--name", "billing", "--budget", "25", "--cache")
    raw = next(word for word in out.split() if word.startswith("lgw_"))

    assert code == 0
    with session_scope() as session:
        key = session.scalar(select(VirtualKey))
        assert key.key_hash == hash_key(raw)
        assert (key.monthly_budget_usd, key.cache_enabled) == (25, True)
    code, listing = lgw("keys", "list")
    assert raw not in listing and "billing" in listing and "25.00" in listing


def test_duplicate_names_and_bad_budgets_are_rejected(lgw) -> None:
    assert lgw("keys", "create", "--name", "a")[0] == 0
    assert lgw("keys", "create", "--name", "a")[0] != 0
    assert lgw("keys", "create", "--name", "b", "--budget", "-1")[0] == 2
    assert lgw("keys", "set-budget", "a", "abc")[0] == 2
    assert lgw("keys", "set-budget", "missing", "5")[0] == 1


def test_budget_change_and_revocation(lgw) -> None:
    lgw("keys", "create", "--name", "app")
    assert lgw("keys", "set-budget", "app", "10")[0] == 0
    assert lgw("keys", "revoke", "app", "--yes")[0] == 0
    with session_scope() as session:
        key = session.scalar(select(VirtualKey))
        assert (key.monthly_budget_usd, key.active) == (10, False)
    assert lgw("keys", "revoke", "nosuch", "--yes")[0] == 1


def test_revoke_asks_for_confirmation(lgw, monkeypatch) -> None:
    lgw("keys", "create", "--name", "app")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    lgw("keys", "revoke", "app")
    with session_scope() as session:
        assert session.scalar(select(VirtualKey.active)) is True


def test_routes_need_a_provider_key_and_a_price(lgw, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    code, out = lgw("routes", "set", "chat", "openai:gpt-4.1-mini")
    assert code != 0 and "OPENAI_API_KEY" in out

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()
    code, out = lgw("routes", "set", "chat", "openai:gpt-4.1-mini")
    assert code != 0 and "no price row" in out

    assert (
        lgw("prices", "add", "openai", "gpt-4.1-mini", "--input", "0.4", "--output", "1.6")[0] == 0
    )
    assert lgw("routes", "set", "chat", "openai:gpt-4.1-mini")[0] == 0
    assert lgw("routes", "set", "chat", "nonsense")[0] != 0
    code, out = lgw("routes", "list")
    get_settings.cache_clear()

    assert "1. openai:gpt-4.1-mini" in out or "openai:gpt-4.1-mini" in out
    with session_scope() as session:
        assert [r.provider_model for r in session.scalars(select(ModelRoute))] == ["gpt-4.1-mini"]


def test_prices_are_append_only_and_listed_newest_first(lgw) -> None:
    lgw(
        "prices",
        "add",
        "openai",
        "m",
        "--input",
        "1",
        "--output",
        "2",
        "--effective-at",
        "2026-01-01T00:00:00Z",
    )
    lgw("prices", "add", "openai", "m", "--input", "3", "--output", "4")
    assert lgw("prices", "add", "openai", "m", "--input", "x", "--output", "4")[0] == 2
    assert (
        lgw(
            "prices",
            "add",
            "openai",
            "m",
            "--input",
            "1",
            "--output",
            "1",
            "--effective-at",
            "yesterday",
        )[0]
        == 2
    )

    code, out = lgw("prices", "list", "--provider", "openai", "--model", "m")
    rows = [line.split() for line in out.splitlines()[1:]]

    assert code == 0
    assert [row[3] for row in rows] == ["3.00", "1.00"]


def test_empty_listings_say_so(lgw) -> None:
    assert "no" in lgw("keys", "list")[1].lower()
    assert "no routes" in lgw("routes", "list")[1]
    assert lgw("prices", "list")[0] == 0


def test_an_unusable_database_url_is_a_usage_error(capsys) -> None:
    assert main(["--db", "notadb://x", "keys", "list"]) == 2
