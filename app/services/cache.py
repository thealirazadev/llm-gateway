"""Opt-in response cache: exact match by prompt hash, then semantic match by embedding.

Entries are scoped to (key, model), hold redacted content only, and expire after a TTL
(docs/architecture.md, "Semantic cache").
"""

import hashlib
import json
import math
import re
from array import array
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import GEMINI_BASE_URL, OPENAI_BASE_URL, get_settings
from app.logging import get_logger
from app.models import CacheEntry
from app.schemas import ChatCompletionRequest

logger = get_logger(__name__)

_PLACEHOLDER = re.compile(r"<pii_(?:email|phone|card)_\d+>")
_EMBED_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CachedAnswer:
    content: str
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int


def prompt_hash(body: ChatCompletionRequest) -> str:
    """SHA-256 over the redacted messages, model, and every sampling parameter."""
    canonical = {
        "model": body.model,
        "messages": [{"role": m.role, "content": m.content} for m in body.messages],
        "temperature": body.temperature,
        "top_p": body.top_p,
        "max_tokens": body.max_tokens,
        "stop": body.stop,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def _unpack(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return values.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _answer(entry: CacheEntry) -> CachedAnswer:
    data = json.loads(entry.response_json)
    return CachedAnswer(
        data["content"], data.get("finish_reason"), entry.prompt_tokens, entry.completion_tokens
    )


def _usable(entry: CacheEntry, placeholders: dict[str, str]) -> bool:
    """A cached answer may only mention placeholders this request can restore."""
    content = json.loads(entry.response_json)["content"]
    return all(found in placeholders for found in _PLACEHOLDER.findall(content))


def lookup(
    session: Session,
    key_id: int,
    body: ChatCompletionRequest,
    digest: str,
    embedding: list[float] | None,
    placeholders: dict[str, str],
    now: datetime,
) -> CachedAnswer | None:
    scope = (CacheEntry.key_id == key_id, CacheEntry.model == body.model)
    entry = session.scalar(
        select(CacheEntry).where(
            *scope, CacheEntry.prompt_hash == digest, CacheEntry.expires_at > now
        )
    )
    if entry is None and embedding is not None:
        settings = get_settings()
        candidates = session.scalars(
            select(CacheEntry)
            .where(*scope, CacheEntry.embedding.is_not(None), CacheEntry.expires_at > now)
            .order_by(CacheEntry.created_at.desc())
            .limit(settings.lgw_cache_scan_limit)
        )
        best, best_score = None, settings.lgw_cache_similarity_threshold
        for candidate in candidates:
            score = cosine(embedding, _unpack(candidate.embedding or b""))
            if score >= best_score:
                best, best_score = candidate, score
        entry = best
    if entry is None or not _usable(entry, placeholders):
        return None
    session.execute(
        update(CacheEntry)
        .where(CacheEntry.id == entry.id)
        .values(hit_count=CacheEntry.hit_count + 1, last_hit_at=now)
    )
    return _answer(entry)


def store(
    session: Session,
    key_id: int,
    body: ChatCompletionRequest,
    digest: str,
    embedding: list[float] | None,
    answer: CachedAnswer,
    now: datetime,
) -> None:
    """Insert an entry, or refresh the existing one for the same prompt."""
    session.execute(
        delete(CacheEntry).where(
            CacheEntry.key_id == key_id,
            CacheEntry.model == body.model,
            CacheEntry.prompt_hash == digest,
        )
    )
    session.add(
        CacheEntry(
            key_id=key_id,
            model=body.model,
            prompt_hash=digest,
            embedding=_pack(embedding) if embedding is not None else None,
            response_json=json.dumps(
                {"content": answer.content, "finish_reason": answer.finish_reason}
            ),
            prompt_tokens=answer.prompt_tokens,
            completion_tokens=answer.completion_tokens,
            hit_count=0,
            created_at=now,
            expires_at=now + timedelta(hours=get_settings().lgw_cache_ttl_hours),
        )
    )


def evict(session: Session, now: datetime) -> int:
    """Drop expired entries, then the oldest beyond each key's cap. Returns rows removed."""
    removed = session.execute(delete(CacheEntry).where(CacheEntry.expires_at <= now)).rowcount
    cap = get_settings().lgw_cache_max_entries_per_key
    for key_id in session.scalars(select(CacheEntry.key_id).distinct()).all():
        keep = (
            select(CacheEntry.id)
            .where(CacheEntry.key_id == key_id)
            .order_by(CacheEntry.created_at.desc(), CacheEntry.id.desc())
            .limit(cap)
        )
        removed += session.execute(
            delete(CacheEntry).where(CacheEntry.key_id == key_id, CacheEntry.id.not_in(keep))
        ).rowcount
    return removed or 0


async def embed(client: httpx.AsyncClient, text: str) -> list[float] | None:
    """Embed redacted text with the configured provider. None turns the semantic layer off."""
    settings = get_settings()
    provider, model = settings.lgw_embedding_provider, settings.lgw_embedding_model
    if not provider or not model:
        return None
    try:
        if provider == "openai" and settings.openai_api_key:
            response = await client.post(
                f"{OPENAI_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": model, "input": text},
                timeout=_EMBED_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return [float(x) for x in response.json()["data"][0]["embedding"]]
        if provider == "gemini" and settings.gemini_api_key:
            response = await client.post(
                f"{GEMINI_BASE_URL}/models/{model}:embedContent",
                headers={"x-goog-api-key": settings.gemini_api_key},
                json={"content": {"parts": [{"text": text}]}},
                timeout=_EMBED_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return [float(x) for x in response.json()["embedding"]["values"]]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("cache.embedding_failed", extra={"provider": provider, "reason": str(exc)})
        return None
    logger.warning("cache.embedding_unconfigured", extra={"provider": provider})
    return None
