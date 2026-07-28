import json
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import Request as RequestRow
from tests.conftest import sse_payloads, sse_upstream

BODY = {
    "model": "claude-sonnet",
    "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hi"},
    ],
}

RESPONSE = {
    "id": "msg_01ABC",
    "type": "message",
    "role": "assistant",
    "model": "claude-3-5-haiku-latest",
    "content": [{"type": "text", "text": "Hello there."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 184, "output_tokens": 96},
}

STREAM = (
    json.dumps(
        {
            "type": "message_start",
            "message": {
                "id": "msg_01ABC",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": 184, "output_tokens": 1},
            },
        }
    ),
    json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
    json.dumps(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello "},
        }
    ),
    json.dumps(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "there."},
        }
    ),
    json.dumps({"type": "content_block_stop", "index": 0}),
    json.dumps(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 96},
        }
    ),
    json.dumps({"type": "message_stop"}),
)


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def route(seed_route, seed_price) -> int:
    seed_route(
        model="claude-sonnet", provider="anthropic", provider_model="claude-3-5-haiku-latest"
    )
    return seed_price(provider="anthropic", provider_model="claude-3-5-haiku-latest")


async def test_anthropic_non_streamed_returns_the_openai_shape(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=RESPONSE)

    price_id = route(seed_route, seed_price)
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "claude-sonnet"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "Hello there."}
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"] == {
        "prompt_tokens": 184,
        "completion_tokens": 96,
        "total_tokens": 280,
    }
    assert response.headers["X-LGW-Provider"] == "anthropic"
    assert seen["payload"]["system"] == "You are terse."
    assert seen["payload"]["max_tokens"] == 1024
    assert seen["headers"]["x-api-key"] == "sk-ant-test"

    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "ok"
    assert row.provider == "anthropic"
    assert row.price_id == price_id
    assert Decimal(row.cost_usd) == Decimal("0.000227")


async def test_anthropic_streamed_returns_openai_chunks_with_reported_usage(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    route(seed_route, seed_price)
    upstream(sse_upstream(*STREAM))

    body = dict(BODY, stream=True)
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)

    assert response.status_code == 200
    payloads = sse_payloads(response.text)
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(payload) for payload in payloads[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    assert text == "Hello there."
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "ok"
    assert row.tokens_estimated is False
    assert row.prompt_tokens == 184
    assert row.completion_tokens == 96
    assert Decimal(row.cost_usd) == Decimal("0.000227")


async def test_anthropic_error_event_mid_stream_terminates_without_done(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    route(seed_route, seed_price)
    upstream(
        sse_upstream(
            *STREAM[:4],
            json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
        )
    )

    body = dict(BODY, stream=True)
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)

    payloads = sse_payloads(response.text)
    assert "[DONE]" not in payloads
    assert json.loads(payloads[-1])["error"]["code"] == "upstream_error"
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "failed"
    assert row.error_code == "upstream_error"


async def test_an_anthropic_stream_that_dies_before_message_delta_is_estimated(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    """`message_start` carries the final input count but only the output count so far.

    Billing a killed stream from it would charge one output token for content the provider
    already generated and label the row as provider-reported usage.
    """
    route(seed_route, seed_price)
    generated = "x" * 400
    upstream(
        sse_upstream(
            STREAM[0],
            STREAM[1],
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": generated},
                }
            ),
            json.dumps({"type": "error", "error": {"type": "api_error", "message": "gone"}}),
        )
    )

    body = dict(BODY, stream=True)
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)

    assert "[DONE]" not in sse_payloads(response.text)
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "failed"
    assert row.prompt_tokens == 184
    assert row.tokens_estimated is True
    assert row.completion_tokens == 100  # ceil(len(generated) / 4)
    assert Decimal(row.cost_usd) == Decimal("0.000234")


async def test_anthropic_400_surfaces_the_upstream_message(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}},
        )

    route(seed_route, seed_price)
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 400
    assert response.json() == {
        "error": {"message": "nope", "type": "invalid_request_error", "code": "upstream_rejected"}
    }
