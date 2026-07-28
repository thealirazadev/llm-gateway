"""Anthropic Messages API adapter.

Translation only: system messages are hoisted to the top-level `system` field, `max_tokens`
is mandatory upstream so it falls back to the configured default, and stop reasons map onto
the canonical OpenAI values.
"""

import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from app.config import ANTHROPIC_API_VERSION, ANTHROPIC_BASE_URL, get_settings
from app.providers.base import (
    OUTCOME_BAD_RESPONSE,
    OUTCOME_CONNECT_ERROR,
    OUTCOME_HTTP_ERROR,
    Provider,
    ProviderCall,
    ProviderFailure,
    ProviderResult,
    StreamEvent,
    sse_data,
)
from app.schemas import ChatCompletionRequest, ChatCompletionResponse

FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def _decode_event(data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ProviderFailure(
            OUTCOME_BAD_RESPONSE, "anthropic sent a malformed stream event."
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(OUTCOME_BAD_RESPONSE, "anthropic sent a malformed stream event.")
    return payload


class AnthropicProvider(Provider):
    name = "anthropic"

    def build_call(self, body: ChatCompletionRequest, provider_model: str) -> ProviderCall:
        settings = get_settings()
        api_key = settings.anthropic_api_key
        if not api_key:
            raise ProviderFailure(OUTCOME_CONNECT_ERROR, "ANTHROPIC_API_KEY is not configured.")
        system = "\n".join(
            str(message.content) for message in body.messages if message.role == "system"
        )
        payload: dict[str, Any] = {
            "model": provider_model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in body.messages
                if message.role != "system"
            ],
            "max_tokens": body.max_tokens or settings.lgw_default_max_output_tokens,
        }
        if system:
            payload["system"] = system
        if body.temperature is not None:
            payload["temperature"] = body.temperature
        if body.top_p is not None:
            payload["top_p"] = body.top_p
        if body.stop is not None:
            payload["stop_sequences"] = [body.stop] if isinstance(body.stop, str) else body.stop
        return ProviderCall(
            url=f"{ANTHROPIC_BASE_URL}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "Content-Type": "application/json",
            },
            payload=payload,
        )

    def build_stream_call(self, body: ChatCompletionRequest, provider_model: str) -> ProviderCall:
        call = self.build_call(body, provider_model)
        return replace(call, payload={**call.payload, "stream": True})

    def parse_response(
        self, payload: dict[str, Any], request_id: str, model: str
    ) -> ProviderResult:
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens", 0))
        completion_tokens = int(usage.get("output_tokens", 0))
        text = "".join(
            str(block.get("text", ""))
            for block in payload.get("content") or []
            if block.get("type") == "text"
        )
        response = ChatCompletionResponse.model_validate(
            {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": FINISH_REASONS.get(payload.get("stop_reason") or ""),
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )
        if not payload.get("content"):
            raise ValueError("response contained no content blocks")
        return ProviderResult(
            response=response,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def translate_stream(self, lines: AsyncIterator[str]) -> AsyncIterator[StreamEvent]:
        """Map Anthropic's typed events onto canonical chunks.

        Input tokens arrive once on `message_start`; the running output count arrives on
        `message_delta`, whose last value is the final one.
        """
        async for data in sse_data(lines):
            payload = _decode_event(data)
            kind = payload.get("type")
            if kind == "message_start":
                usage = (payload.get("message") or {}).get("usage") or {}
                yield StreamEvent(
                    role="assistant",
                    # `message_start` reports the final input count but only the output count
                    # so far (typically 1). Recording that as reported usage would bill a
                    # stream that dies before `message_delta` for one output token and mark it
                    # as provider-reported; leaving it unset keeps the estimate fallback.
                    prompt_tokens=(
                        int(usage["input_tokens"]) if "input_tokens" in usage else None
                    ),
                )
            elif kind == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield StreamEvent(content=str(delta.get("text", "")))
            elif kind == "message_delta":
                usage = payload.get("usage") or {}
                stop_reason = (payload.get("delta") or {}).get("stop_reason")
                yield StreamEvent(
                    # An unrecognised stop reason still terminates the stream for the client.
                    finish_reason=FINISH_REASONS.get(stop_reason, "stop") if stop_reason else None,
                    completion_tokens=(
                        int(usage["output_tokens"]) if "output_tokens" in usage else None
                    ),
                )
            elif kind == "error":
                error = payload.get("error") or {}
                raise ProviderFailure(
                    OUTCOME_HTTP_ERROR,
                    str(error.get("message") or "anthropic failed mid-stream."),
                )

    def translate_error(self, status_code: int, payload: dict[str, Any] | None) -> str:
        error = (payload or {}).get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])
        return f"The upstream provider returned status {status_code}."


PROVIDER = AnthropicProvider()
