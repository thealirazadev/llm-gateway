"""App factory: logging, request ids, error envelope, routes, startup validation."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import Response
from sqlalchemy import select

from app.config import PROVIDER_KEY_FIELDS, provider_api_key
from app.db import session_scope
from app.errors import REQUEST_ID_HEADER, register_exception_handlers
from app.logging import configure_logging, get_logger, new_request_id, request_id_var
from app.models import ModelRoute, utcnow
from app.routes import chat, health, models
from app.services.budget import sweep_stale_reservations
from app.services.cache import evict
from app.services.cost import resolve_price

Handler = Callable[[Request], Awaitable[Response]]

logger = get_logger(__name__)


SWEEP_INTERVAL_SECONDS = 60


def validate_configuration() -> list[str]:
    """Every active route must be servable: known provider, credential present, price row.

    An un-costable or uncallable request must be impossible at runtime, so the process
    refuses to start instead of discovering the gap on a paid request.
    """
    problems: list[str] = []
    with session_scope() as session:
        routes = list(
            session.scalars(
                select(ModelRoute)
                .where(ModelRoute.active.is_(True))
                .order_by(ModelRoute.model, ModelRoute.position)
            )
        )
        for route in routes:
            target = f"{route.provider}:{route.provider_model}"
            if route.provider not in PROVIDER_KEY_FIELDS:
                problems.append(
                    f"model '{route.model}' routes to unknown provider '{route.provider}'"
                )
                continue
            if not provider_api_key(route.provider):
                problems.append(
                    f"{route.provider.upper()}_API_KEY is not configured but model "
                    f"'{route.model}' routes to {target}"
                )
            if resolve_price(session, route.provider, route.provider_model) is None:
                problems.append(f"no price row for {target}, routed by model '{route.model}'")
    return problems


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    problems = validate_configuration()
    if problems:
        for problem in problems:
            logger.error("startup.invalid", extra={"problem": problem})
        raise RuntimeError("startup validation failed: " + "; ".join(problems))
    logger.info("startup.validated")
    sweeper = asyncio.create_task(_sweep_forever())
    yield
    sweeper.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper


async def _sweep_forever() -> None:
    """Free holds left by requests that never settled, and prune expired or excess cache rows."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            sweep_stale_reservations()
        except Exception:  # a transient database error must not stop future sweeps
            logger.exception("budget.sweep_failed")
        try:
            with session_scope() as session:
                evict(session, utcnow())
        except Exception:
            logger.exception("cache.evict_failed")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="llm-gateway", version="0.2.0", lifespan=lifespan)
    register_exception_handlers(app)

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next: Handler) -> Response:
        request_id = new_request_id()
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers.setdefault(REQUEST_ID_HEADER, request_id)
        return response

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(models.router)
    return app


app = create_app()
