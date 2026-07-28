"""Provider interface and the shared httpx client.

Every adapter translates the canonical OpenAI shapes to and from one upstream wire format.
Nothing outside this package knows a provider's request or response layout.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.logging import get_logger
from app.schemas import ChatCompletionRequest, ChatCompletionResponse

logger = get_logger(__name__)

UPSTREAM_EXCERPT_CHARS = 512

# Attempt outcomes recorded in the attempts table.
OUTCOME_OK = "ok"
OUTCOME_HTTP_ERROR = "http_error"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_CONNECT_ERROR = "connect_error"
OUTCOME_BAD_RESPONSE = "bad_response"

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Shared connection pool. Holds no request state; tests swap it for a mock transport."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.lgw_upstream_timeout_seconds,
                connect=settings.lgw_connect_timeout_seconds,
            )
        )
    return _client


def set_client(client: httpx.AsyncClient | None) -> None:
    global _client
    _client = client


class ProviderFailure(Exception):
    """One failed upstream attempt, classified for the attempts row and for failover."""

    def __init__(
        self,
        outcome: str,
        message: str,
        status_code: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.message = message
        self.status_code = status_code
        self.latency_ms = latency_ms


@dataclass(frozen=True)
class ProviderCall:
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ProviderResult:
    response: ChatCompletionResponse
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    name: str

    @abstractmethod
    def build_call(self, body: ChatCompletionRequest, provider_model: str) -> ProviderCall:
        """Translate a canonical request into one upstream HTTP call."""

    @abstractmethod
    def parse_response(
        self, payload: dict[str, Any], request_id: str, model: str
    ) -> ProviderResult:
        """Translate an upstream response body into the canonical response shape."""

    @abstractmethod
    def translate_error(self, status_code: int, payload: dict[str, Any] | None) -> str:
        """Extract a client-safe message from an upstream error body."""


def _json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def call_provider(
    provider: Provider,
    body: ChatCompletionRequest,
    provider_model: str,
    request_id: str,
) -> ProviderResult:
    """Run one upstream attempt. Every failure mode becomes a classified ProviderFailure."""
    call = provider.build_call(body, provider_model)
    started = time.perf_counter()
    try:
        response = await get_client().post(call.url, headers=call.headers, json=call.payload)
    except httpx.TimeoutException as exc:
        raise ProviderFailure(
            OUTCOME_TIMEOUT,
            f"{provider.name} timed out.",
            latency_ms=int((time.perf_counter() - started) * 1000),
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderFailure(
            OUTCOME_CONNECT_ERROR,
            f"Could not reach {provider.name}.",
            latency_ms=int((time.perf_counter() - started) * 1000),
        ) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        payload = _json_or_none(response)
        logger.warning(
            "upstream.error",
            extra={
                "provider": provider.name,
                "status_code": response.status_code,
                "excerpt": response.text[:UPSTREAM_EXCERPT_CHARS],
            },
        )
        outcome = OUTCOME_RATE_LIMITED if response.status_code == 429 else OUTCOME_HTTP_ERROR
        raise ProviderFailure(
            outcome,
            provider.translate_error(response.status_code, payload),
            status_code=response.status_code,
            latency_ms=latency_ms,
        )

    payload = _json_or_none(response)
    if payload is None:
        raise ProviderFailure(
            OUTCOME_BAD_RESPONSE,
            f"{provider.name} returned a malformed response.",
            latency_ms=latency_ms,
        )
    try:
        result = provider.parse_response(payload, request_id, body.model)
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "upstream.bad_response",
            extra={"provider": provider.name, "reason": str(exc)[:UPSTREAM_EXCERPT_CHARS]},
        )
        raise ProviderFailure(
            OUTCOME_BAD_RESPONSE,
            f"{provider.name} returned a malformed response.",
            latency_ms=latency_ms,
        ) from exc
    return replace(result, latency_ms=latency_ms)
