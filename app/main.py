"""App factory: logging, request ids, error envelope, routes."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.errors import REQUEST_ID_HEADER, register_exception_handlers
from app.logging import configure_logging, new_request_id, request_id_var
from app.routes import chat, health

Handler = Callable[[Request], Awaitable[Response]]


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="llm-gateway", version="0.1.0")
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
    return app


app = create_app()
