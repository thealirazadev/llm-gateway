"""Priority failover across a model's routes.

One attempt per route and no per-route retry: the client owns end-to-end retries. Every
attempt writes its row before failover proceeds, so the audit shows what was tried, and only
the route that actually answered contributes cost. Failover happens strictly before any byte
reaches the client, which is what makes it impossible to bill two providers for one request.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.errors import (
    GatewayError,
    model_not_found,
    upstream_error,
    upstream_rate_limited,
    upstream_rejected,
)
from app.logging import get_logger
from app.models import Attempt, ModelRoute, utcnow
from app.providers import PROVIDERS
from app.providers.base import (
    OUTCOME_HTTP_ERROR,
    OUTCOME_RATE_LIMITED,
    Provider,
    ProviderFailure,
)

logger = get_logger(__name__)

T = TypeVar("T")


def resolve_routes(model: str) -> list[ModelRoute]:
    """Every active route for a public model, in priority order."""
    with session_scope() as session:
        routes = list(
            session.scalars(
                select(ModelRoute)
                .where(ModelRoute.model == model, ModelRoute.active.is_(True))
                .order_by(ModelRoute.position)
            )
        )
    if not routes:
        raise model_not_found(model)
    usable = [route for route in routes if route.provider in PROVIDERS]
    if not usable:
        logger.error(
            "route.provider_unavailable",
            extra={"model": model, "providers": sorted({route.provider for route in routes})},
        )
        raise upstream_error()
    return usable


def record_attempt(
    session: Session,
    request_id: str,
    route: ModelRoute,
    outcome: str,
    status_code: int | None,
    latency_ms: int | None,
) -> None:
    """Attempt rows are immutable: one row per upstream call, written once."""
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


def _can_failover(failure: ProviderFailure) -> bool:
    """Timeout, connect error, malformed body, 5xx, and 429 are worth another route.

    Any other 4xx means the request itself is bad for that provider, so trying the next one
    would only produce the same rejection with extra latency.
    """
    if failure.outcome != OUTCOME_HTTP_ERROR:
        return True
    return failure.status_code is None or failure.status_code >= 500


def _exhaustion_error(failures: Sequence[ProviderFailure]) -> GatewayError:
    if failures and all(failure.outcome == OUTCOME_RATE_LIMITED for failure in failures):
        return upstream_rate_limited()
    return upstream_error()


async def run_attempts(
    routes: Sequence[ModelRoute],
    request_id: str,
    action: Callable[[Provider, ModelRoute], Awaitable[T]],
) -> tuple[ModelRoute, T]:
    """Try each route once until one answers; return the winning route and its result."""
    failures: list[ProviderFailure] = []
    for route in routes:
        try:
            value = await action(PROVIDERS[route.provider], route)
        except ProviderFailure as failure:
            with session_scope() as session:
                record_attempt(
                    session,
                    request_id,
                    route,
                    failure.outcome,
                    failure.status_code,
                    failure.latency_ms,
                )
            logger.warning(
                "failover.attempt_failed",
                extra={
                    "request_id": request_id,
                    "position": route.position,
                    "provider": route.provider,
                    "provider_model": route.provider_model,
                    "outcome": failure.outcome,
                    "status_code": failure.status_code,
                },
            )
            if not _can_failover(failure):
                raise upstream_rejected(failure.message) from failure
            failures.append(failure)
            continue
        return route, value
    raise _exhaustion_error(failures)
