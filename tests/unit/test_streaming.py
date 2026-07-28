"""Chunk framing and the streaming placeholder restoration buffer."""

import json

import pytest

from app.schemas import ChatCompletionChunk, ChunkChoice, ChunkDelta, Usage, chunk_json
from app.services.streaming import RestorationBuffer

MAP = {"<pii_email_1>": "jane@example.com", "<pii_email_10>": "bob@example.com"}


def test_chunk_framing_matches_the_documented_shape() -> None:
    chunk = ChatCompletionChunk(
        id="chatcmpl-01J2",
        created=1753600000,
        model="gpt-4.1-mini",
        choices=[ChunkChoice(index=0, delta=ChunkDelta(role="assistant"))],
    )
    payload = json.loads(chunk_json(chunk))

    assert payload["object"] == "chat.completion.chunk"
    assert payload["choices"][0]["delta"] == {"role": "assistant"}
    assert payload["choices"][0]["finish_reason"] is None
    assert "usage" not in payload


def test_finish_chunk_carries_an_empty_delta() -> None:
    chunk = ChatCompletionChunk(
        id="chatcmpl-01J2",
        created=1,
        model="m",
        choices=[ChunkChoice(delta=ChunkDelta(), finish_reason="stop")],
    )
    payload = json.loads(chunk_json(chunk))
    assert payload["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}


def test_usage_chunk_has_no_choices() -> None:
    chunk = ChatCompletionChunk(
        id="chatcmpl-01J2",
        created=1,
        model="m",
        usage=Usage(prompt_tokens=184, completion_tokens=96, total_tokens=280),
    )
    payload = json.loads(chunk_json(chunk))
    assert payload["choices"] == []
    assert payload["usage"] == {
        "prompt_tokens": 184,
        "completion_tokens": 96,
        "total_tokens": 280,
    }


def test_zero_redaction_requests_bypass_the_buffer() -> None:
    buffer = RestorationBuffer({})
    pieces = ["a <pii_", "email_1> b"]
    assert [buffer.push(piece) for piece in pieces] == pieces
    assert buffer.flush() == ""


def test_a_whole_placeholder_in_one_chunk_is_restored() -> None:
    buffer = RestorationBuffer(MAP)
    assert buffer.push("Mail <pii_email_1> now.") == "Mail jane@example.com now."


@pytest.mark.parametrize("split", range(1, len("Mail <pii_email_1> now.")))
def test_a_placeholder_split_at_any_offset_is_restored(split: int) -> None:
    text = "Mail <pii_email_1> now."
    buffer = RestorationBuffer(MAP)
    out = buffer.push(text[:split]) + buffer.push(text[split:]) + buffer.flush()
    assert out == "Mail jane@example.com now."


def test_a_placeholder_split_across_three_chunks_is_restored() -> None:
    buffer = RestorationBuffer(MAP)
    out = buffer.push("<pii_") + buffer.push("email") + buffer.push("_1> ok") + buffer.flush()
    assert out == "jane@example.com ok"


def test_the_longer_placeholder_wins_when_one_is_a_prefix_of_another() -> None:
    buffer = RestorationBuffer(MAP)
    out = buffer.push("<pii_email_1") + buffer.push("0> hi") + buffer.flush()
    assert out == "bob@example.com hi"


def test_placeholders_absent_from_the_map_pass_through_untouched() -> None:
    buffer = RestorationBuffer(MAP)
    out = buffer.push("see <pii_card_9> and <pii_") + buffer.push("phone_2>") + buffer.flush()
    assert out == "see <pii_card_9> and <pii_phone_2>"


def test_a_stream_ending_on_a_partial_placeholder_flushes_it_verbatim() -> None:
    buffer = RestorationBuffer(MAP)
    assert buffer.push("half <pii_ema") == "half "
    assert buffer.flush() == "<pii_ema"
    assert buffer.flush() == ""


def test_the_buffer_never_holds_more_than_the_longest_placeholder() -> None:
    buffer = RestorationBuffer(MAP)
    long_tail = "x" * 200
    assert buffer.push(long_tail) == long_tail
