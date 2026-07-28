"""Adapter translation against recorded upstream wire shapes."""

import pytest

from app.providers.anthropic import PROVIDER as ANTHROPIC
from app.providers.gemini import PROVIDER as GEMINI
from app.schemas import ChatCompletionRequest

REQUEST = ChatCompletionRequest.model_validate(
    {
        "model": "claude-sonnet",
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Again"},
        ],
        "temperature": 0.2,
        "top_p": 0.9,
        "stop": "END",
    }
)

ANTHROPIC_RESPONSE = {
    "id": "msg_01ABC",
    "type": "message",
    "role": "assistant",
    "model": "claude-3-5-haiku-latest",
    "content": [{"type": "text", "text": "Hello there."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 184, "output_tokens": 96},
}


def test_anthropic_hoists_system_and_requires_max_tokens() -> None:
    call = ANTHROPIC.build_call(REQUEST, "claude-3-5-haiku-latest")

    assert call.url.endswith("/messages")
    assert call.headers["x-api-key"] == "sk-ant-test"
    assert call.headers["anthropic-version"] == "2023-06-01"
    assert call.payload["system"] == "You are terse."
    assert call.payload["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Again"},
    ]
    assert call.payload["max_tokens"] == 1024
    assert call.payload["temperature"] == 0.2
    assert call.payload["top_p"] == 0.9
    assert call.payload["stop_sequences"] == ["END"]
    assert "stream" not in call.payload


def test_anthropic_stream_call_sets_stream() -> None:
    call = ANTHROPIC.build_stream_call(REQUEST, "claude-3-5-haiku-latest")
    assert call.payload["stream"] is True


def test_anthropic_client_max_tokens_wins() -> None:
    body = REQUEST.model_copy(update={"max_tokens": 300})
    assert ANTHROPIC.build_call(body, "claude-3-5-haiku-latest").payload["max_tokens"] == 300


def test_anthropic_response_becomes_the_canonical_shape() -> None:
    result = ANTHROPIC.parse_response(ANTHROPIC_RESPONSE, "01J2ZK", "claude-sonnet")

    assert result.response.id == "chatcmpl-01J2ZK"
    assert result.response.model == "claude-sonnet"
    assert result.response.choices[0].message.role == "assistant"
    assert result.response.choices[0].message.content == "Hello there."
    assert result.response.choices[0].finish_reason == "stop"
    assert result.prompt_tokens == 184
    assert result.completion_tokens == 96
    assert result.response.usage.total_tokens == 280


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("end_turn", "stop"), ("max_tokens", "length"), ("stop_sequence", "stop"), ("weird", None)],
)
def test_anthropic_finish_reasons_map_to_openai_values(stop_reason: str, expected: str) -> None:
    payload = dict(ANTHROPIC_RESPONSE, stop_reason=stop_reason)
    result = ANTHROPIC.parse_response(payload, "01J2ZK", "claude-sonnet")
    assert result.response.choices[0].finish_reason == expected


def test_anthropic_empty_content_is_a_bad_response() -> None:
    with pytest.raises(ValueError):
        ANTHROPIC.parse_response(dict(ANTHROPIC_RESPONSE, content=[]), "01J2ZK", "claude-sonnet")


def test_anthropic_error_message_is_extracted() -> None:
    payload = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad model"}}
    assert ANTHROPIC.translate_error(400, payload) == "bad model"
    assert "503" in ANTHROPIC.translate_error(503, None)


GEMINI_RESPONSE = {
    "candidates": [
        {
            "content": {"parts": [{"text": "Hello "}, {"text": "there."}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 184,
        "candidatesTokenCount": 96,
        "totalTokenCount": 280,
    },
}


def test_gemini_hoists_system_and_renames_the_assistant_role() -> None:
    call = GEMINI.build_call(REQUEST, "gemini-2.0-flash")

    assert call.url.endswith("/models/gemini-2.0-flash:generateContent")
    assert call.headers["x-goog-api-key"] == "gemini-test"
    assert call.payload["systemInstruction"] == {"parts": [{"text": "You are terse."}]}
    assert call.payload["contents"] == [
        {"role": "user", "parts": [{"text": "Hello"}]},
        {"role": "model", "parts": [{"text": "Hi"}]},
        {"role": "user", "parts": [{"text": "Again"}]},
    ]
    assert call.payload["generationConfig"] == {
        "maxOutputTokens": 1024,
        "temperature": 0.2,
        "topP": 0.9,
        "stopSequences": ["END"],
    }


def test_gemini_stream_call_uses_the_sse_endpoint() -> None:
    call = GEMINI.build_stream_call(REQUEST, "gemini-2.0-flash")
    assert call.url.endswith("/models/gemini-2.0-flash:streamGenerateContent?alt=sse")


def test_gemini_response_becomes_the_canonical_shape() -> None:
    result = GEMINI.parse_response(GEMINI_RESPONSE, "01J2ZK", "gemini-flash")

    assert result.response.id == "chatcmpl-01J2ZK"
    assert result.response.choices[0].message.content == "Hello there."
    assert result.response.choices[0].finish_reason == "stop"
    assert result.prompt_tokens == 184
    assert result.completion_tokens == 96


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("STOP", "stop"), ("MAX_TOKENS", "length"), ("SAFETY", "content_filter"), ("OTHER", None)],
)
def test_gemini_finish_reasons_map_to_openai_values(reason: str, expected: str) -> None:
    payload = {
        "candidates": [dict(GEMINI_RESPONSE["candidates"][0], finishReason=reason)],
        "usageMetadata": GEMINI_RESPONSE["usageMetadata"],
    }
    assert (
        GEMINI.parse_response(payload, "01J2ZK", "g").response.choices[0].finish_reason == expected
    )


def test_gemini_without_candidates_is_a_bad_response() -> None:
    with pytest.raises(ValueError):
        GEMINI.parse_response({"candidates": []}, "01J2ZK", "gemini-flash")


def test_gemini_error_message_is_extracted() -> None:
    payload = {"error": {"code": 400, "message": "bad request", "status": "INVALID_ARGUMENT"}}
    assert GEMINI.translate_error(400, payload) == "bad request"
    assert "503" in GEMINI.translate_error(503, None)
