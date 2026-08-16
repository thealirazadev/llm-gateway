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
from sqlalchemy.orm import Session

from app.auth import require_key
from app.config import get_settings
from app.db import session_scope
from app.errors import (
    GatewayError,
    budget_exceeded,
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
from app.providers.base import (
    OUTCOME_OK,
    OUTCOME_RATE_LIMITED,
    ProviderFailure,
    ProviderResult,
    call_provider,
    open_stream,
)
from app.providers import PROVIDERS
from app.schemas import ChatCompletionRequest, validate_supported
from app.services.budget import commit_reservation, release_reservation, reserve
from app.services.cost import compute_cost, estimate_tokens, resolve_price
from app.services.router import resolve_routes, run_attempts
from app.services.streaming import stream_completion
from app.schemas import prompt_text

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




def _create_request_row(
    request_id: str, key: VirtualKey, body: ChatCompletionRequest
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

    estimate_usd = (prompt_tokens / 1000000 * max_input_price) + (
        output_tokens / 1000000 * max_output_price
    )
    return estimate_usd


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
                provider=route.provider,
                provider_model=route.provider_model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_usd=cost,
                price_id=price_id,
                latency_ms=latency_ms,
            )
        )
    return cost




@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    key: Annotated[VirtualKey, Depends(require_key)],
) -> Response:
    body = await _parse_request(request)
    request_id: str = request.state.request_id
    routes = resolve_routes(body.model)

    try:
        _create_request_row(request_id, key, body)
    except SQLAlchemyError as exc:
        logger.error("audit.create_failed", extra={"reason": str(exc)})
        raise server_error() from exc

    cache_state = "miss" if key.cache_enabled else "off"
    started = time.perf_counter()

    if body.stream:
        async def stream_action(provider, route: ModelRoute):
            upstream = await open_stream(provider, body, route.provider_model)
            return stream_completion(
                provider, upstream, body, route, request_id, cache_state, started
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
