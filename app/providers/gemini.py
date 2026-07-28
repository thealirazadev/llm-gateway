"""Gemini generateContent adapter.

Translation only: system messages become `systemInstruction`, the assistant role becomes
`model`, sampling options move into `generationConfig`, and finish reasons map onto the
canonical OpenAI values.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from app.config import GEMINI_BASE_URL, get_settings
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
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
}


def _text_of(candidate: dict[str, Any]) -> str:
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(str(part.get("text", "")) for part in parts)


def _decode_chunk(data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ProviderFailure(
            OUTCOME_BAD_RESPONSE, "gemini sent a malformed stream chunk."
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(OUTCOME_BAD_RESPONSE, "gemini sent a malformed stream chunk.")
    return payload


class GeminiProvider(Provider):
    name = "gemini"

    def _payload(self, body: ChatCompletionRequest) -> dict[str, Any]:
        settings = get_settings()
        system = "\n".join(
            str(message.content) for message in body.messages if message.role == "system"
        )
        generation: dict[str, Any] = {
            "maxOutputTokens": body.max_tokens or settings.lgw_default_max_output_tokens
        }
        if body.temperature is not None:
            generation["temperature"] = body.temperature
        if body.top_p is not None:
            generation["topP"] = body.top_p
        if body.stop is not None:
            generation["stopSequences"] = [body.stop] if isinstance(body.stop, str) else body.stop
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": [{"text": message.content}],
                }
                for message in body.messages
                if message.role != "system"
            ],
            "generationConfig": generation,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def _headers(self) -> dict[str, str]:
        api_key = get_settings().gemini_api_key
        if not api_key:
            raise ProviderFailure(OUTCOME_CONNECT_ERROR, "GEMINI_API_KEY is not configured.")
        return {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    def build_call(self, body: ChatCompletionRequest, provider_model: str) -> ProviderCall:
        return ProviderCall(
            url=f"{GEMINI_BASE_URL}/models/{provider_model}:generateContent",
            headers=self._headers(),
            payload=self._payload(body),
        )

    def build_stream_call(self, body: ChatCompletionRequest, provider_model: str) -> ProviderCall:
        return ProviderCall(
            url=f"{GEMINI_BASE_URL}/models/{provider_model}:streamGenerateContent?alt=sse",
            headers=self._headers(),
            payload=self._payload(body),
        )

    def parse_response(
        self, payload: dict[str, Any], request_id: str, model: str
    ) -> ProviderResult:
        candidates = payload.get("candidates") or []
        if not candidates:
            raise ValueError("response contained no candidates")
        usage = payload.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount", 0))
        completion_tokens = int(usage.get("candidatesTokenCount", 0))
        response = ChatCompletionResponse.model_validate(
            {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": _text_of(candidates[0])},
                        "finish_reason": FINISH_REASONS.get(
                            candidates[0].get("finishReason") or ""
                        ),
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )
        return ProviderResult(
            response=response,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def translate_stream(self, lines: AsyncIterator[str]) -> AsyncIterator[StreamEvent]:
        """Map `streamGenerateContent` chunks onto canonical chunks.

        Gemini repeats `usageMetadata` on chunks with cumulative counts, so the last one seen
        is the authoritative usage. The opening role delta has no Gemini equivalent and is
        synthesised once, before any content.
        """
        role_sent = False
        async for data in sse_data(lines):
            payload = _decode_chunk(data)
            error = payload.get("error")
            if isinstance(error, dict):
                raise ProviderFailure(
                    OUTCOME_HTTP_ERROR,
                    str(error.get("message") or "gemini failed mid-stream."),
                )
            usage = payload.get("usageMetadata") or {}
            if not role_sent:
                role_sent = True
                yield StreamEvent(role="assistant")
            for candidate in payload.get("candidates") or []:
                text = _text_of(candidate)
                finish = candidate.get("finishReason")
                if text:
                    yield StreamEvent(content=text)
                if finish:
                    yield StreamEvent(finish_reason=FINISH_REASONS.get(finish, "stop"))
            if usage:
                yield StreamEvent(
                    prompt_tokens=int(usage.get("promptTokenCount", 0)),
                    completion_tokens=int(usage.get("candidatesTokenCount", 0)),
                )

    def translate_error(self, status_code: int, payload: dict[str, Any] | None) -> str:
        error = (payload or {}).get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])
        return f"The upstream provider returned status {status_code}."


PROVIDER = GeminiProvider()
