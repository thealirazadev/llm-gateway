"""SSE relay with accounting.

The upstream head is validated before a single byte reaches the client, so a stream rejected
upstream fails exactly like a non-streaming attempt. Once the first chunk is written the
attempt is committed: there is no mid-stream provider switch, and the cost of tokens actually
generated is recorded whether the stream completes or dies upstream.
"""

import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal

import httpx
from fastapi.responses import StreamingResponse
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask

from app.db import session_scope
from app.errors import envelope, upstream_error
from app.logging import get_logger
from app.models import Attempt, ModelRoute, utcnow
from app.models import Request as RequestRow
from app.providers.base import (
    OUTCOME_CONNECT_ERROR,
    OUTCOME_OK,
    OUTCOME_TIMEOUT,
    Provider,
    ProviderFailure,
    open_stream,
)
from app.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChunkChoice,
    ChunkDelta,
    Usage,
    chunk_json,
    prompt_text,
)
from app.services.cost import compute_cost, estimate_tokens, resolve_price

logger = get_logger(__name__)

SSE_DONE = b"data: [DONE]\n\n"

# Every placeholder is shorter than this, which bounds how much text the restoration
# buffer may ever hold back.
MAX_PLACEHOLDER_CHARS = 24

_STATUS_EVENTS = {
    "ok": "request.completed",
    "failed": "request.failed",
    "cut_off": "request.cut_off",
}


class RestorationBuffer:
    """Holds back trailing text that could still turn out to be a placeholder.

    A placeholder split across two upstream chunks would otherwise reach the client
    unrestored, so any suffix that is a proper prefix of a known placeholder is kept until the
    next chunk completes it or the stream ends. Requests with nothing redacted bypass the
    buffer entirely, so the common path adds no latency.
    """

    def __init__(self, placeholders: Mapping[str, str]) -> None:
        self._placeholders = dict(placeholders)
        self._hold_limit = min(
            MAX_PLACEHOLDER_CHARS,
            max((len(key) for key in self._placeholders), default=0),
        )
        self._held = ""

    def push(self, text: str) -> str:
        if not self._placeholders:
            return text
        combined = self._held + text
        keep = self._pending_length(combined)
        cut = len(combined) - keep
        self._held = combined[cut:]
        return self._restore(combined[:cut])

    def flush(self) -> str:
        held, self._held = self._held, ""
        return self._restore(held)

    def _pending_length(self, text: str) -> int:
        for size in range(min(len(text), self._hold_limit), 0, -1):
            suffix = text[-size:]
            if any(key.startswith(suffix) and len(key) > size for key in self._placeholders):
                return size
        return 0

    def _restore(self, text: str) -> str:
        for placeholder, value in self._placeholders.items():
            text = text.replace(placeholder, value)
        return text


@dataclass
class _StreamState:
    # A stream that neither completes nor fails upstream was cut off by the client.
    status: str = "cut_off"
    outcome: str = OUTCOME_OK
    error_code: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    text: str = ""
    ttfb_ms: int | None = None


def _resolve_usage(state: _StreamState, body: ChatCompletionRequest) -> tuple[int, int, bool]:
    """Provider-reported usage always wins; a stream that died before reporting is estimated."""
    estimated = state.prompt_tokens is None or state.completion_tokens is None
    prompt_tokens = (
        state.prompt_tokens
        if state.prompt_tokens is not None
        else estimate_tokens(prompt_text(body))
    )
    completion_tokens = (
        state.completion_tokens
        if state.completion_tokens is not None
        else estimate_tokens(state.text)
    )
    return prompt_tokens, completion_tokens, estimated


def _persist(
    route: ModelRoute,
    request_id: str,
    body: ChatCompletionRequest,
    state: _StreamState,
    started: float,
) -> None:
    """Write the attempt row and finalize the request row, cost included, exactly once."""
    prompt_tokens, completion_tokens, estimated = _resolve_usage(state, body)
    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        with session_scope() as session:
            price = resolve_price(session, route.provider, route.provider_model)
            if price is None:
                logger.error(
                    "prices.missing",
                    extra={
                        "request_id": request_id,
                        "provider": route.provider,
                        "provider_model": route.provider_model,
                    },
                )
                cost = Decimal("0")
                price_id = None
            else:
                cost = compute_cost(price, prompt_tokens, completion_tokens)
                price_id = price.id
            session.add(
                Attempt(
                    request_id=request_id,
                    position=route.position,
                    provider=route.provider,
                    provider_model=route.provider_model,
                    outcome=state.outcome,
                    status_code=200,
                    latency_ms=latency_ms,
                    created_at=utcnow(),
                )
            )
            session.execute(
                update(RequestRow)
                .where(RequestRow.id == request_id, RequestRow.status == "in_flight")
                .values(
                    status=state.status,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    tokens_estimated=estimated,
                    cost_usd=cost,
                    price_id=price_id,
                    latency_ms=latency_ms,
                    ttfb_ms=state.ttfb_ms,
                    error_code=state.error_code,
                )
            )
    except SQLAlchemyError as exc:
        logger.error("audit.finalize_failed", extra={"request_id": request_id, "reason": str(exc)})
        return
    logger.info(
        _STATUS_EVENTS[state.status],
        extra={
            "request_id": request_id,
            "provider": route.provider,
            "provider_model": route.provider_model,
            "streamed": True,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tokens_estimated": estimated,
            "cost_usd": str(cost),
            "ttfb_ms": state.ttfb_ms,
            "latency_ms": latency_ms,
        },
    )


async def stream_completion(
    provider: Provider,
    body: ChatCompletionRequest,
    route: ModelRoute,
    request_id: str,
    cache_state: str,
    placeholders: Mapping[str, str] | None = None,
) -> StreamingResponse:
    """Open the upstream stream, relay it as OpenAI chunks, and account for it exactly once."""
    started = time.perf_counter()
    upstream = await open_stream(provider, body, route.provider_model)
    state = _StreamState()
    buffer = RestorationBuffer(placeholders or {})
    created = int(time.time())
    include_usage = bool(body.stream_options and body.stream_options.include_usage)

    def frame(choices: list[ChunkChoice], usage: Usage | None = None) -> bytes:
        chunk = ChatCompletionChunk(
            id=f"chatcmpl-{request_id}",
            created=created,
            model=body.model,
            choices=choices,
            usage=usage,
        )
        return f"data: {chunk_json(chunk)}\n\n".encode()

    def delta_frame(delta: ChunkDelta, finish_reason: str | None = None) -> bytes:
        return frame([ChunkChoice(index=0, delta=delta, finish_reason=finish_reason)])

    def error_frame(message: str | None) -> bytes:
        error = upstream_error(message) if message else upstream_error()
        return f"data: {json.dumps(envelope(error), separators=(',', ':'))}\n\n".encode()

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for event in provider.translate_stream(upstream.aiter_lines()):
                if state.ttfb_ms is None:
                    state.ttfb_ms = int((time.perf_counter() - started) * 1000)
                if event.prompt_tokens is not None:
                    state.prompt_tokens = event.prompt_tokens
                if event.completion_tokens is not None:
                    state.completion_tokens = event.completion_tokens
                if event.role is not None:
                    yield delta_frame(ChunkDelta(role=event.role))
                if event.content:
                    state.text += event.content
                    released = buffer.push(event.content)
                    if released:
                        yield delta_frame(ChunkDelta(content=released))
                if event.finish_reason is not None:
                    tail = buffer.flush()
                    if tail:
                        yield delta_frame(ChunkDelta(content=tail))
                    yield delta_frame(ChunkDelta(), finish_reason=event.finish_reason)
            tail = buffer.flush()
            if tail:
                yield delta_frame(ChunkDelta(content=tail))
            if include_usage:
                prompt_tokens, completion_tokens, _ = _resolve_usage(state, body)
                yield frame(
                    [],
                    Usage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    ),
                )
            yield SSE_DONE
            state.status = "ok"
        except ProviderFailure as failure:
            state.status = "failed"
            state.outcome = failure.outcome
            state.error_code = "upstream_error"
            yield error_frame(failure.message)
        except httpx.HTTPError as exc:
            state.status = "failed"
            state.outcome = (
                OUTCOME_TIMEOUT
                if isinstance(exc, httpx.TimeoutException)
                else OUTCOME_CONNECT_ERROR
            )
            state.error_code = "upstream_error"
            yield error_frame(None)

    async def finish() -> None:
        """Close upstream and account for the stream however it ended.

        A client that disappears mid-stream leaves the relay suspended forever, so this
        cannot live in the generator: Starlette cancels the send task and runs the response
        background task afterwards, which is the one point every ending passes through.
        Tokens already generated were billed by the provider and are never refunded, so the
        row is finalized `cut_off` with the cost observed so far.
        """
        await _close(upstream, request_id)
        _persist(route, request_id, body, state, started)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-LGW-Provider": route.provider,
            "X-LGW-Cache": cache_state,
        },
        background=BackgroundTask(finish),
    )


async def _close(upstream: httpx.Response, request_id: str) -> None:
    try:
        await upstream.aclose()
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("stream.close_failed", extra={"request_id": request_id, "reason": str(exc)})
