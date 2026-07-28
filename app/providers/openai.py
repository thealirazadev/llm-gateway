"""OpenAI adapter. The canonical format is OpenAI's, so this adapter is a passthrough."""

from typing import Any

from app.config import OPENAI_BASE_URL, get_settings
from app.providers.base import (
    OUTCOME_CONNECT_ERROR,
    Provider,
    ProviderCall,
    ProviderFailure,
    ProviderResult,
)
from app.schemas import ChatCompletionRequest, ChatCompletionResponse


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
