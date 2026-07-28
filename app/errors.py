"""The one error envelope, its codes, and the handlers that emit it.

Every JSON error the gateway returns has the OpenAI shape
`{"error": {"message", "type", "code"}}` so client SDKs raise their native exceptions.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_logger, request_id_var

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-LGW-Request-Id"


class GatewayError(Exception):
    """An error with a documented status, code, and client-safe message."""

    def __init__(self, status_code: int, code: str, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.error_type = error_type
        self.message = message


def invalid_api_key(message: str = "Invalid or revoked API key.") -> GatewayError:
    return GatewayError(401, "invalid_api_key", "invalid_request_error", message)


def model_not_found(model: str) -> GatewayError:
    return GatewayError(
        404,
        "model_not_found",
        "invalid_request_error",
        f"No active route is configured for model '{model}'.",
    )


def unsupported_parameter(message: str) -> GatewayError:
    return GatewayError(400, "unsupported_parameter", "invalid_request_error", message)


def invalid_request(message: str) -> GatewayError:
    return GatewayError(400, "invalid_request", "invalid_request_error", message)


def upstream_rejected(message: str) -> GatewayError:
    return GatewayError(400, "upstream_rejected", "invalid_request_error", message)


def payload_too_large(limit_kb: int) -> GatewayError:
    return GatewayError(
        413,
        "payload_too_large",
        "invalid_request_error",
        f"Request body exceeds the {limit_kb} KB limit.",
    )


def upstream_rate_limited() -> GatewayError:
    return GatewayError(
        429,
        "upstream_rate_limited",
        "rate_limit_error",
        "The upstream provider rate limited this request.",
    )


def upstream_error(message: str = "The upstream provider is unavailable.") -> GatewayError:
    return GatewayError(502, "upstream_error", "api_error", message)


def server_error() -> GatewayError:
    return GatewayError(500, "server_error", "api_error", "Internal gateway error.")


def envelope(error: GatewayError) -> dict[str, dict[str, str]]:
    return {"error": {"message": error.message, "type": error.error_type, "code": error.code}}


def error_response(error: GatewayError) -> JSONResponse:
    headers = {}
    request_id = request_id_var.get()
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(status_code=error.status_code, content=envelope(error), headers=headers)


def from_validation_error(exc: ValidationError | RequestValidationError) -> GatewayError:
    """Unknown or forbidden fields are unsupported parameters; anything else is a bad schema."""
    errors = exc.errors()
    for item in errors:
        if item.get("type") == "extra_forbidden":
            field = ".".join(str(part) for part in item.get("loc", ()))
            return unsupported_parameter(f"Unsupported parameter: '{field}'.")
    if errors:
        first = errors[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "body"
        return invalid_request(f"Invalid request: '{field}' {first.get('msg', 'is invalid')}.")
    return invalid_request("Invalid request body.")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        logger.warning(
            "request.rejected",
            extra={"error_code": exc.code, "status_code": exc.status_code},
        )
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(from_validation_error(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        mapped = GatewayError(exc.status_code, "invalid_request", "invalid_request_error", detail)
        if exc.status_code >= 500:
            mapped = server_error()
        return error_response(mapped)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Details go to the log only; the client never sees internals.
        logger.exception("request.error", extra={"error_class": type(exc).__name__})
        return error_response(server_error())
