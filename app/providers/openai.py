"""OpenAI adapter. The canonical format is OpenAI's, so this adapter is a passthrough."""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from app.config import OPENAI_BASE_URL, get_settings
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


def _decode_chunk(data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ProviderFailure(
            OUTCOME_BAD_RESPONSE, "openai sent a malformed stream chunk."
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(OUTCOME_BAD_RESPONSE, "openai sent a malformed stream chunk.")
    return payload


class OpenAIProvider(Provider):
    name = "openai"

    def build_call(self, body: ChatCompletionRequest, provider_model: str) -> ProviderCall:
        api_key = get_settings().openai_api_key
        if not api_key:
            raise ProviderFailure(OUTCOME_CONNECT_ERROR, "OPENAI_API_KEY is not configured.")
        payload: dict[str, Any] = {
            "model": provider_model,
            "messages": [
                {"role": message.role, "content": message.content} for message in body.messages
            ],
        }
        for field_name in ("temperature", "top_p", "max_tokens", "stop"):
            value = getattr(body, field_name)
            if value is not None:
                payload[field_name] = value
        return ProviderCall(
            url=f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload=payload,
        )

    def build_stream_call(self, body: ChatCompletionRequest, provider_model: str) -> ProviderCall:
        call = self.build_call(body, provider_model)
        payload = dict(call.payload)
        payload["stream"] = True
        # Usage is requested upstream unconditionally: the gateway must account for every
        # stream even when the client did not ask to see the usage chunk.
        payload["stream_options"] = {"include_usage": True}
        return replace(call, payload=payload)

    async def translate_stream(self, lines: AsyncIterator[str]) -> AsyncIterator[StreamEvent]:
        async for data in sse_data(lines):
            if data == "[DONE]":
                return
            payload = _decode_chunk(data)
            error = payload.get("error")
            if isinstance(error, dict):
                raise ProviderFailure(
                    OUTCOME_HTTP_ERROR,
                    str(error.get("message") or "openai failed mid-stream."),
                )
            usage = payload.get("usage")
            if isinstance(usage, dict):
                yield StreamEvent(
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                )
            for choice in payload.get("choices") or []:
                delta = choice.get("delta") or {}
                event = StreamEvent(
                    role=delta.get("role"),
                    content=delta.get("content"),
                    finish_reason=choice.get("finish_reason"),
                )
                if event.role or event.content is not None or event.finish_reason:
                    yield event

    def parse_response(
        self, payload: dict[str, Any], request_id: str, model: str
    ) -> ProviderResult:
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        response = ChatCompletionResponse.model_validate(
            {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": payload.get("created", 0),
                "model": model,
                "choices": [
                    {
                        "index": choice.get("index", index),
                        "message": {
                            "role": choice.get("message", {}).get("role", "assistant"),
                            "content": choice.get("message", {}).get("content"),
                        },
                        "finish_reason": choice.get("finish_reason"),
                    }
                    for index, choice in enumerate(payload.get("choices") or [])
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": int(
                        usage.get("total_tokens", prompt_tokens + completion_tokens)
                    ),
                },
            }
        )
        if not response.choices:
            raise ValueError("response contained no choices")
        return ProviderResult(
            response=response,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def translate_error(self, status_code: int, payload: dict[str, Any] | None) -> str:
        error = (payload or {}).get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])
        return f"The upstream provider returned status {status_code}."


PROVIDER = OpenAIProvider()
