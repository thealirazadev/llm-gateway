"""Shared fixtures. Every test runs against a fresh SQLite file built by the migrations,
and never touches the network: upstreams are httpx.MockTransport handlers."""

import os
import shutil
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from app.auth import generate_key
from app.config import get_settings
from app.db import build_engine, session_scope, set_engine
from app.models import ModelRoute, Price, VirtualKey, utcnow
from app.providers import base

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _test_environment() -> Iterator[None]:
    os.environ["OPENAI_API_KEY"] = "sk-test-openai"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the migrations once per session; tests copy the result."""
    template = tmp_path_factory.mktemp("template") / "template.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{template}")
    command.upgrade(config, "head")
    return template


@pytest.fixture
def db(migrated_template: Path, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "gateway.db"
    shutil.copyfile(migrated_template, path)
    engine = build_engine(f"sqlite:///{path}")
    set_engine(engine)
    yield path
    engine.dispose()


@pytest.fixture
def api_key(db: Path) -> str:
    raw_key, key_hash, last4 = generate_key()
    with session_scope() as session:
        session.add(
            VirtualKey(
                name="test-key",
                key_hash=key_hash,
                key_last4=last4,
                cache_enabled=False,
                active=True,
                created_at=utcnow(),
            )
        )
    return raw_key


@pytest.fixture
def seed_route(db: Path) -> Callable[..., None]:
    def _seed(
        model: str = "gpt-4.1-mini",
        provider: str = "openai",
        provider_model: str = "gpt-4.1-mini",
        position: int = 1,
    ) -> None:
        with session_scope() as session:
            session.add(
                ModelRoute(
                    model=model,
                    position=position,
                    provider=provider,
                    provider_model=provider_model,
                    active=True,
                )
            )

    return _seed


@pytest.fixture
def seed_price(db: Path) -> Callable[..., int]:
    def _seed(
        provider: str = "openai",
        provider_model: str = "gpt-4.1-mini",
        input_usd: str = "0.40",
        output_usd: str = "1.60",
        effective_at: datetime | None = None,
    ) -> int:
        with session_scope() as session:
            price = Price(
                provider=provider,
                provider_model=provider_model,
                input_usd_per_mtok=Decimal(input_usd),
                output_usd_per_mtok=Decimal(output_usd),
                effective_at=effective_at or (utcnow() - timedelta(days=1)),
                created_at=utcnow(),
            )
            session.add(price)
            session.flush()
            return int(price.id)

    return _seed


@pytest.fixture
def upstream() -> Iterator[Callable[[Callable[[httpx.Request], httpx.Response]], None]]:
    """Install a mock upstream for the shared provider client."""

    def _install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        base.set_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    yield _install
    base.set_client(None)


@pytest_asyncio.fixture
async def client(db: Path) -> httpx.AsyncClient:
    from app.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as async_client:
        yield async_client


def openai_response(
    content: str = "Hello there.",
    prompt_tokens: int = 184,
    completion_tokens: int = 96,
) -> dict:
    return {
        "id": "chatcmpl-upstream",
        "object": "chat.completion",
        "created": 1753600000,
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
