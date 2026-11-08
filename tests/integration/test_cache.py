import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.auth import generate_key
from app.config import get_settings
from app.db import session_scope
from app.models import CacheEntry, VirtualKey, utcnow
from app.models import Request as RequestRow
from app.services import cache
from tests.conftest import (
    openai_chunk,
    openai_response,
    openai_usage_chunk,
    sse_payloads,
    sse_upstream,
)


def make_key(cache_enabled: bool = True) -> str:
    raw, key_hash, last4 = generate_key()
    with session_scope() as session:
        session.add(
            VirtualKey(
                name=f"k{last4}",
                key_hash=key_hash,
                key_last4=last4,
                cache_enabled=cache_enabled,
                active=True,
                created_at=utcnow(),
            )
        )
    return raw


def auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def body(content: str = "What is our refund window?", stream: bool = False) -> dict:
    return {
        "model": "gpt-4.1-mini",
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
    }


@pytest.fixture
def counting_upstream(upstream, seed_route, seed_price):
    seed_route()
    seed_price()
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            text = json.loads(request.content)["input"]
            calls.append({"embed": text})
            # Deterministic fake: refund questions point one way, everything else another.
            vector = [1.0, 0.05] if "refund" in text.lower() else [0.0, 1.0]
            return httpx.Response(200, json={"data": [{"embedding": vector}]})
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=openai_response("30 days.", 12, 3))

    upstream(handler)
    return calls


async def test_an_identical_request_is_served_from_the_cache_for_free(
    client: httpx.AsyncClient, counting_upstream
) -> None:
    key = make_key()
    first = await client.post("/v1/chat/completions", headers=auth(key), json=body())
    second = await client.post("/v1/chat/completions", headers=auth(key), json=body())

    assert first.headers["X-LGW-Cache"] == "miss"
    assert second.headers["X-LGW-Cache"] == "hit"
    assert second.json()["choices"][0]["message"]["content"] == "30 days."
    assert second.json()["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
    }
    assert len(counting_upstream) == 1
    with session_scope() as session:
        row = session.get(RequestRow, second.headers["X-LGW-Request-Id"])
        assert (row.status, row.cache_hit, row.cost_usd) == ("ok", True, 0)


async def test_cache_is_off_without_opt_in_and_never_crosses_keys(
    client: httpx.AsyncClient, counting_upstream
) -> None:
    off, a, b = make_key(cache_enabled=False), make_key(), make_key()
    for _ in range(2):
        response = await client.post("/v1/chat/completions", headers=auth(off), json=body())
        assert response.headers["X-LGW-Cache"] == "off"
    await client.post("/v1/chat/completions", headers=auth(a), json=body())
    other = await client.post("/v1/chat/completions", headers=auth(b), json=body())

    assert other.headers["X-LGW-Cache"] == "miss"
    assert len(counting_upstream) == 4


async def test_a_paraphrase_hits_semantically_and_an_unrelated_prompt_misses(
    client: httpx.AsyncClient, counting_upstream, monkeypatch
) -> None:
    monkeypatch.setenv("LGW_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("LGW_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()
    key = make_key()
    await client.post("/v1/chat/completions", headers=auth(key), json=body())
    paraphrase = await client.post(
        "/v1/chat/completions", headers=auth(key), json=body("How long do refunds take?")
    )
    unrelated = await client.post(
        "/v1/chat/completions", headers=auth(key), json=body("Do you ship to Canada?")
    )
    get_settings.cache_clear()

    assert paraphrase.headers["X-LGW-Cache"] == "hit"
    assert unrelated.headers["X-LGW-Cache"] == "miss"
    chat_calls = [c for c in counting_upstream if "messages" in c]
    assert len(chat_calls) == 2


async def test_a_cached_answer_naming_an_unknown_placeholder_is_a_miss(
    client: httpx.AsyncClient, upstream, seed_route, seed_price
) -> None:
    seed_route()
    seed_price()
    replies = iter(["Sent to <pii_email_1>.", "Fresh answer."])
    upstream(lambda r: httpx.Response(200, json=openai_response(next(replies), 5, 2)))
    key = make_key()
    prompt = "Email me at ana@example.com"
    first = await client.post("/v1/chat/completions", headers=auth(key), json=body(prompt))
    # Same redacted prompt and same placeholder: exact hit, restored with this request's value.
    again = await client.post(
        "/v1/chat/completions", headers=auth(key), json=body("Email me at bo@example.com")
    )

    assert first.json()["choices"][0]["message"]["content"] == "Sent to ana@example.com."
    assert again.headers["X-LGW-Cache"] == "hit"
    assert again.json()["choices"][0]["message"]["content"] == "Sent to bo@example.com."
    with session_scope() as session:
        stored = session.scalar(select(CacheEntry.response_json))
    assert "ana@example.com" not in stored and "<pii_email_1>" in stored

    with session_scope() as session:
        entry = session.scalar(select(CacheEntry))
        assert cache._usable(entry, {}) is False


async def test_a_streamed_request_is_cached_and_replayed_as_a_stream(
    client: httpx.AsyncClient, upstream, seed_route, seed_price
) -> None:
    seed_route()
    seed_price()
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": ""}),
            openai_chunk({"content": "Thirty "}),
            openai_chunk({"content": "days."}),
            openai_chunk({}, finish_reason="stop"),
            openai_usage_chunk(20, 4),
            "[DONE]",
        )
    )
    key = make_key()
    await client.post("/v1/chat/completions", headers=auth(key), json=body(stream=True))
    replay = await client.post(
        "/v1/chat/completions",
        headers=auth(key),
        json={**body(stream=True), "stream_options": {"include_usage": True}},
    )

    payloads = sse_payloads(replay.text)
    assert replay.headers["X-LGW-Cache"] == "hit"
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(p) for p in payloads[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Thirty days."
    assert chunks[-1]["usage"]["completion_tokens"] == 4


def test_eviction_drops_expired_entries_and_the_oldest_beyond_the_cap(db, monkeypatch) -> None:
    monkeypatch.setenv("LGW_CACHE_MAX_ENTRIES_PER_KEY", "2")
    get_settings.cache_clear()
    raw = make_key()
    now = utcnow()
    with session_scope() as session:
        key_id = session.scalar(select(VirtualKey.id).where(VirtualKey.key_last4 == raw[-4:]))
        for n in range(4):
            session.add(
                CacheEntry(
                    key_id=key_id,
                    model="m",
                    prompt_hash=f"{n:064d}",
                    response_json='{"content": "x"}',
                    prompt_tokens=1,
                    completion_tokens=1,
                    hit_count=0,
                    created_at=now - timedelta(minutes=10 - n),
                    expires_at=now + (timedelta(hours=1) if n else -timedelta(seconds=1)),
                )
            )
    with session_scope() as session:
        removed = cache.evict(session, now)
        left = session.scalars(select(CacheEntry.prompt_hash).order_by(CacheEntry.id)).all()
        assert session.scalar(select(func.count(CacheEntry.id))) == 2
    get_settings.cache_clear()

    assert removed == 2
    assert left == [f"{2:064d}", f"{3:064d}"]


def test_cosine_and_vector_packing() -> None:
    assert cache.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cache.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cache.cosine([1.0], [1.0, 2.0]) == 0.0
    assert cache.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cache._unpack(cache._pack([0.5, -1.25])) == [0.5, -1.25]


async def test_an_embedding_outage_degrades_to_a_normal_upstream_call(
    client: httpx.AsyncClient, upstream, seed_route, seed_price, monkeypatch
) -> None:
    seed_route()
    seed_price()
    monkeypatch.setenv("LGW_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("LGW_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(503, json={})
        return httpx.Response(200, json=openai_response("ok", 1, 1))

    upstream(handler)
    response = await client.post("/v1/chat/completions", headers=auth(make_key()), json=body())
    get_settings.cache_clear()

    assert response.status_code == 200
    assert response.headers["X-LGW-Cache"] == "miss"
    with session_scope() as session:
        assert session.scalar(select(CacheEntry.embedding)) is None
