"""Canonical wire shapes. The OpenAI chat format is both the client contract and the
internal representation; providers translate to and from these models."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.errors import unsupported_parameter

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    # Typed loosely so image content parts can be rejected as an unsupported parameter
    # rather than as a schema violation.
    content: Any


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    stop: str | list[str] | None = None
    # Declared so they can be rejected with the documented code instead of extra_forbidden.
    n: int | None = None
    tools: list[Any] | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ResponseMessage(BaseModel):
    role: str
    content: str | None = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


def validate_supported(body: ChatCompletionRequest) -> None:
    """Reject the documented non-goals: tools, n > 1, and non-text content parts."""
    if body.tools:
        raise unsupported_parameter("Unsupported parameter: 'tools' is not supported.")
    if body.n is not None and body.n != 1:
        raise unsupported_parameter("Unsupported parameter: 'n' must be 1.")
    for index, message in enumerate(body.messages):
        if not isinstance(message.content, str):
            raise unsupported_parameter(
                f"Unsupported parameter: 'messages.{index}.content' must be a string."
            )


def prompt_text(body: ChatCompletionRequest) -> str:
    return "\n".join(str(message.content) for message in body.messages)
