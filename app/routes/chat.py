"""Chat completions: authenticate, route, call upstream, cost, audit. Streamed or not."""

import json
import time
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_key
from app.config import get_settings
from app.db import session_scope
from app.errors import (
    GatewayError,
    budget_exceeded,
    from_validation_error,
    invalid_request,
    payload_too_large,
    server_error,
)
from app.logging import get_logger
from app.models import Attempt, ModelRoute, VirtualKey, utcnow
from app.models import Request as RequestRow
from app.providers.base import (
    OUTCOME_OK,
    ProviderResult,
    call_provider,
    get_client,
    open_stream,
)
from app.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChunkChoice,
    ChunkDelta,
    ResponseMessage,
    Usage,
    chunk_json,
    prompt_text,
    validate_supported,
)
from app.services import cache
from app.services.budget import reserve, settle_request
from app.services.cost import compute_cost, estimate_tokens, resolve_price
from app.services.redaction import Redactor, restore
from app.services.router import resolve_routes, run_attempts
from app.services.streaming import stream_completion

router = APIRouter()
logger = get_logger(__name__)


async def _parse_request(request: Request) -> ChatCompletionRequest:
    limit_kb = get_settings().lgw_max_body_kb
    limit_bytes = limit_kb * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit_bytes:
        raise payload_too_large(limit_kb)
    raw = await request.body()
    if len(raw) > limit_bytes:
        raise payload_too_large(limit_kb)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise invalid_request("Request body is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise invalid_request("Request body must be a JSON object.")
    try:
        body = ChatCompletionRequest.model_validate(data)
    except ValidationError as exc:
        raise from_validation_error(exc) from exc
    validate_supported(body)
    return body


def _redact(body: ChatCompletionRequest) -> tuple[ChatCompletionRequest, Redactor]:
    """Redact every message before anything else sees the prompt; the map stays in memory."""
    redactor = Redactor()
    messages = [
        message.model_copy(update={"content": redactor.redact(message.content)})
        for message in body.messages
    ]
    return body.model_copy(update={"messages": messages}), redactor


def _create_request_row(
    request_id: str, key: VirtualKey, body: ChatCompletionRequest, redactions: int = 0
) -> None:
    with session_scope() as session:
        session.add(
            RequestRow(
                id=request_id,
                key_id=key.id,
                model=body.model,
                provider=None,
                provider_model=None,
                status="in_flight",
                streamed=body.stream,
                cache_hit=False,
                redactions=redactions,
                created_at=utcnow(),
            )
        )


def _estimate_budget(
    session: Session, body: ChatCompletionRequest, routes: list[ModelRoute]
) -> Decimal | None:
    """Estimate the worst-case cost across all candidate routes."""
    if not routes:
        return None

    prompt_tokens = estimate_tokens(prompt_text(body))
    output_tokens = body.max_tokens or get_settings().lgw_default_max_output_tokens

    # Get prices for all routes and use the highest.
    max_input_price = Decimal("0")
    max_output_price = Decimal("0")

    for route in routes:
        price = resolve_price(session, route.provider, route.provider_model)
        if price:
            if price.input_usd_per_mtok > max_input_price:
                max_input_price = price.input_usd_per_mtok
            if price.output_usd_per_mtok > max_output_price:
                max_output_price = price.output_usd_per_mtok

    per_token = Decimal(1_000_000)
    return (
        Decimal(prompt_tokens) * max_input_price + Decimal(output_tokens) * max_output_price
    ) / (per_token)


def _hold_budget(
    request_id: str, key: VirtualKey, body: ChatCompletionRequest, routes: list[ModelRoute]
) -> None:
    """Hold the worst-case cost against the key's monthly budget before any upstream call."""
    if key.monthly_budget_usd is None:
        return
    expires_at = utcnow() + timedelta(seconds=get_settings().lgw_reservation_ttl_seconds)
    with session_scope() as session:
        estimate = _estimate_budget(session, body, routes) or Decimal("0")
        held = reserve(session, request_id, key.id, key.monthly_budget_usd, estimate, expires_at)
    if held is None:
        raise budget_exceeded()


def _record_attempt(
    request_id: str,
    route: ModelRoute,
    outcome: str,
    status_code: int | None,
    latency_ms: int | None,
) -> None:
    with session_scope() as session:
        session.add(
            Attempt(
                request_id=request_id,
                position=route.position,
                provider=route.provider,
                provider_model=route.provider_model,
                outcome=outcome,
                status_code=status_code,
                latency_ms=latency_ms,
                created_at=utcnow(),
            )
        )


def _finalize(request_id: str, values: dict[str, Any]) -> None:
    """State transitions always name the expected prior state, never set it blindly."""
    with session_scope() as session:
        session.execute(
            update(RequestRow)
            .where(RequestRow.id == request_id, RequestRow.status == "in_flight")
            .values(**values)
        )
        settle_request(session, request_id, Decimal("0"))


def _commit_success(
    request_id: str, route: ModelRoute, result: ProviderResult, latency_ms: int
) -> Decimal:
    with session_scope() as session:
        price = resolve_price(session, route.provider, route.provider_model)
        if price is None:
            logger.error(
                "prices.missing",
                extra={"provider": route.provider, "provider_model": route.provider_model},
            )
            cost = Decimal("0")
            price_id = None
        else:
            cost = compute_cost(price, result.prompt_tokens, result.completion_tokens)
            price_id = price.id
        session.execute(
            update(RequestRow)
            .where(RequestRow.id == request_id, RequestRow.status == "in_flight")
            .values(
                status="ok",
                provider=route.provider,
                provider_model=route.provider_model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_usd=cost,
                price_id=price_id,
                latency_ms=latency_ms,
            )
        )
        settle_request(session, request_id, cost)
    return cost


class _CacheContext:
    """The cache inputs for one opted-in request: its prompt hash and, once needed, embedding."""

    def __init__(self, key: VirtualKey, body: ChatCompletionRequest) -> None:
        self.key_id = key.id
        self.body = body
        self.digest = cache.prompt_hash(body)
        self.embedding: list[float] | None = None

    def _lookup(self, placeholders: dict[str, str]) -> cache.CachedAnswer | None:
        with session_scope() as session:
            return cache.lookup(
                session,
                self.key_id,
                self.body,
                self.digest,
                self.embedding,
                placeholders,
                utcnow(),
            )

    async def find(self, placeholders: dict[str, str]) -> cache.CachedAnswer | None:
        hit = self._lookup(placeholders)
        if hit is None:
            self.embedding = await cache.embed(get_client(), prompt_text(self.body))
            if self.embedding is not None:
                hit = self._lookup(placeholders)
        return hit

    def save(self, answer: cache.CachedAnswer) -> None:
        try:
            with session_scope() as session:
                cache.store(
                    session, self.key_id, self.body, self.digest, self.embedding, answer, utcnow()
                )
        except SQLAlchemyError as exc:
            logger.warning("cache.store_failed", extra={"reason": str(exc)})


def _serve_hit(
    request_id: str,
    body: ChatCompletionRequest,
    hit: cache.CachedAnswer,
    placeholders: dict[str, str],
    started: float,
) -> Response:
    """Answer from the cache: zero cost, no upstream call, reservation released."""
    _finalize(
        request_id,
        {
            "status": "ok",
            "cache_hit": True,
            "prompt_tokens": hit.prompt_tokens,
            "completion_tokens": hit.completion_tokens,
            "cost_usd": Decimal("0"),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        },
    )
    content = restore(hit.content, placeholders)
    usage = Usage(
        prompt_tokens=hit.prompt_tokens,
        completion_tokens=hit.completion_tokens,
        total_tokens=hit.prompt_tokens + hit.completion_tokens,
    )
    headers = {"X-LGW-Provider": "cache", "X-LGW-Cache": "hit"}
    created = int(time.time())
    if not body.stream:
        response = ChatCompletionResponse(
            id=f"chatcmpl-{request_id}",
            created=created,
            model=body.model,
            choices=[
                Choice(
                    message=ResponseMessage(role="assistant", content=content),
                    finish_reason=hit.finish_reason,
                )
            ],
            usage=usage,
        )
        return JSONResponse(content=response.model_dump(), headers=headers)

    def frame(delta: ChunkDelta | None, finish: str | None = None, with_usage: bool = False) -> str:
        chunk = ChatCompletionChunk(
            id=f"chatcmpl-{request_id}",
            created=created,
            model=body.model,
            choices=[] if delta is None else [ChunkChoice(delta=delta, finish_reason=finish)],
            usage=usage if with_usage else None,
        )
        return f"data: {chunk_json(chunk)}\n\n"

    frames = [
        frame(ChunkDelta(role="assistant")),
        frame(ChunkDelta(content=content)),
        frame(ChunkDelta(), hit.finish_reason or "stop"),
    ]
    if body.stream_options and body.stream_options.include_usage:
        frames.append(frame(None, with_usage=True))
    frames.append("data: [DONE]\n\n")

    async def replay():
        for item in frames:
            yield item.encode()

    return StreamingResponse(
        replay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", **headers},
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    key: Annotated[VirtualKey, Depends(require_key)],
) -> Response:
    body, redactor = _redact(await _parse_request(request))
    request_id: str = request.state.request_id
    routes = resolve_routes(body.model)

    try:
        _create_request_row(request_id, key, body, redactor.count)
    except SQLAlchemyError as exc:
        logger.error("audit.create_failed", extra={"reason": str(exc)})
        raise server_error() from exc

    try:
        _hold_budget(request_id, key, body, routes)
    except GatewayError as error:
        _finalize(request_id, {"status": "failed", "error_code": error.code, "latency_ms": 0})
        raise

    cache_state = "miss" if key.cache_enabled else "off"
    started = time.perf_counter()
    cached = _CacheContext(key, body) if key.cache_enabled else None
    if cached is not None:
        try:
            hit = await cached.find(redactor.placeholders)
        except SQLAlchemyError as exc:
            logger.warning("cache.lookup_failed", extra={"reason": str(exc)})
            hit = None
        if hit is not None:
            return _serve_hit(request_id, body, hit, redactor.placeholders, started)

    if body.stream:

        async def stream_action(provider, route: ModelRoute):
            upstream = await open_stream(provider, body, route.provider_model)
            return stream_completion(
                provider,
                upstream,
                body,
                route,
                request_id,
                cache_state,
                started,
                redactor.placeholders,
                on_complete=cached.save if cached is not None else None,
            )

        try:
            route, response = await run_attempts(routes, request_id, stream_action)
            return response
        except GatewayError as error:
            _finalize(
                request_id,
                {
                    "status": "failed",
                    "error_code": error.code,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            raise

    async def call_action(provider, route: ModelRoute):
        return await call_provider(provider, body, route.provider_model, request_id)

    try:
        route, result = await run_attempts(routes, request_id, call_action)
    except GatewayError as error:
        _finalize(
            request_id,
            {
                "status": "failed",
                "error_code": error.code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        raise

    latency_ms = int((time.perf_counter() - started) * 1000)
    _record_attempt(request_id, route, OUTCOME_OK, 200, result.latency_ms)
    cost = _commit_success(request_id, route, result, latency_ms)
    if cached is not None and result.response.choices:
        choice = result.response.choices[0]
        cached.save(
            cache.CachedAnswer(
                choice.message.content or "",
                choice.finish_reason,
                result.prompt_tokens,
                result.completion_tokens,
            )
        )
    logger.info(
        "request.completed",
        extra={
            "provider": route.provider,
            "provider_model": route.provider_model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": str(cost),
            "latency_ms": latency_ms,
        },
    )
    content = result.response.model_dump()
    if redactor.placeholders:
        for choice in content.get("choices", []):
            message = choice.get("message") or {}
            if message.get("content"):
                message["content"] = restore(message["content"], redactor.placeholders)
    return JSONResponse(
        content=content,
        headers={"X-LGW-Provider": route.provider, "X-LGW-Cache": cache_state},
    )
