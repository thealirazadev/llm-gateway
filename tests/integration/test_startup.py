import asyncio

import pytest

from app import main as app_main
from app.config import get_settings
from app.main import create_app, lifespan, validate_configuration


def test_a_servable_configuration_has_no_problems(db, seed_route, seed_price, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()
    seed_route()
    seed_price()
    assert validate_configuration() == []
    get_settings.cache_clear()


def test_each_unservable_route_is_named(db, seed_route, seed_price, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    seed_route(model="a", provider="anthropic", provider_model="claude-x")
    seed_route(model="b", provider="mystery", provider_model="m")
    problems = validate_configuration()
    get_settings.cache_clear()

    assert any("ANTHROPIC_API_KEY" in p for p in problems)
    assert any("no price row for anthropic:claude-x" in p for p in problems)
    assert any("unknown provider 'mystery'" in p for p in problems)


async def test_the_process_refuses_to_start_with_an_unservable_route(db, seed_route, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    seed_route()
    with pytest.raises(RuntimeError, match="startup validation failed"):
        async with lifespan(create_app()):
            pass
    get_settings.cache_clear()


async def test_the_sweeper_survives_database_errors(db, monkeypatch):
    calls: list[str] = []

    def failing_sweep() -> None:
        calls.append("sweep")
        raise RuntimeError("database is locked")

    def failing_evict(session, now) -> int:
        calls.append("evict")
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app_main, "SWEEP_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(app_main, "sweep_stale_reservations", failing_sweep)
    monkeypatch.setattr(app_main, "evict", failing_evict)
    task = asyncio.create_task(app_main._sweep_forever())
    while calls.count("evict") < 2:
        await asyncio.sleep(0)
    task.cancel()

    assert calls[:4] == ["sweep", "evict", "sweep", "evict"]


async def test_a_clean_start_runs_and_stops_the_sweeper(db):
    async with lifespan(create_app()):
        pass
