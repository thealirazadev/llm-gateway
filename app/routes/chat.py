"""Chat completions: authenticate, route, call upstream, cost, audit. Streamed or not."""

import json
import time
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.auth import require_key
from app.config import get_settings
from app.db import session_scope
from app.errors import (
    GatewayError,
    from_validation_error,
    invalid_request,
    model_not_found,
    payload_too_large,
    server_error,
    upstream_error,
    upstream_rate_limited,
    upstream_rejected,
)
from app.logging import get_logger
from app.models import Attempt, ModelRoute, VirtualKey, utcnow
from app.models import Request as RequestRow
from app.providers.anthropic import PROVIDER as ANTHROPIC_PROVIDER
from app.providers.base import (
    OUTCOME_OK,
    OUTCOME_RATE_LIMITED,
    ProviderFailure,
    ProviderResult,
    call_provider,
)
from app.providers.openai import PROVIDER as OPENAI_PROVIDER
from app.schemas import ChatCompletionRequest, validate_supported
from app.services.cost import compute_cost, resolve_price
from app.services.streaming import stream_completion

router = APIRouter()
logger = get_logger(__name__)

PROVIDERS = {
    OPENAI_PROVIDER.name: OPENAI_PROVIDER,
    ANTHROPIC_PROVIDER.name: ANTHROPIC_PROVIDER,
}


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


def _resolve_route(model: str) -> ModelRoute:
    with session_scope() as session:
        route = session.scalar(
            select(ModelRoute)
            .where(ModelRoute.model == model, ModelRoute.active.is_(True))
            .order_by(ModelRoute.position)
            .limit(1)
        )
    if route is None:
        raise model_not_found(model)
    if route.provider not in PROVIDERS:
        logger.error("route.provider_unavailable", extra={"provider": route.provider})
        raise upstream_error()
    return route


def _create_request_row(
    request_id: str, key: VirtualKey, route: ModelRoute, body: ChatCompletionRequest
) -> None:
    with session_scope() as session:
        session.add(
            RequestRow(
                id=request_id,
                key_id=key.id,
                model=body.model,
                provider=route.provider,
                provider_model=route.provider_model,
                status="in_flight",
                streamed=body.stream,
                cache_hit=False,
                created_at=utcnow(),
            )
        )


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
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_usd=cost,
                price_id=price_id,
                latency_ms=latency_ms,
            )
        )
    return cost


def _failure_to_error(failure: ProviderFailure) -> GatewayError:
    if failure.outcome == OUTCOME_RATE_LIMITED:
        return upstream_rate_limited()
    if failure.status_code is not None and 400 <= failure.status_code < 500:
        return upstream_rejected(failure.message)
    return upstream_error()


def _record_failure(
    request_id: str, route: ModelRoute, failure: ProviderFailure, started: float
) -> GatewayError:
    """Record one failed attempt, finalize the request row, and map the failure to an error."""
    error = _failure_to_error(failure)
    _record_attempt(request_id, route, failure.outcome, failure.status_code, failure.latency_ms)
    _finalize(
        request_id,
        {
            "status": "failed",
            "error_code": error.code,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        },
    )
    logger.warning(
        "request.failed",
        extra={
            "provider": route.provider,
            "outcome": failure.outcome,
            "error_code": error.code,
        },
    )
    return error


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    key: Annotated[VirtualKey, Depends(require_key)],
) -> Response:
    body = await _parse_request(request)
    request_id: str = request.state.request_id
    route = _resolve_route(body.model)

    try:
        _create_request_row(request_id, key, route, body)
    except SQLAlchemyError as exc:
        logger.error("audit.create_failed", extra={"reason": str(exc)})
        raise server_error() from exc

    provider = PROVIDERS[route.provider]
    cache_state = "miss" if key.cache_enabled else "off"
    started = time.perf_counter()

    if body.stream:
        try:
            return await stream_completion(provider, body, route, request_id, cache_state)
        except ProviderFailure as failure:
            raise _record_failure(request_id, route, failure, started) from failure

    try:
        result = await call_provider(provider, body, route.provider_model, request_id)
    except ProviderFailure as failure:
        raise _record_failure(request_id, route, failure, started) from failure

    latency_ms = int((time.perf_counter() - started) * 1000)
    _record_attempt(request_id, route, OUTCOME_OK, 200, result.latency_ms)
    cost = _commit_success(request_id, route, result, latency_ms)
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
    return JSONResponse(
        content=result.response.model_dump(),
        headers={"X-LGW-Provider": route.provider, "X-LGW-Cache": cache_state},
    )
