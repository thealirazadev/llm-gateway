import json
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import Request as RequestRow
from tests.conftest import sse_payloads, sse_upstream

BODY = {
    "model": "gemini-flash",
    "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hi"},
    ],
}

RESPONSE = {
    "candidates": [
        {
            "content": {"parts": [{"text": "Hello there."}], "role": "model"},
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


def gemini_chunk(text: str, finish: str | None = None, usage: dict | None = None) -> str:
    chunk: dict = {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}, "index": 0}]
    }
    if finish:
        chunk["candidates"][0]["finishReason"] = finish
    if usage:
        chunk["usageMetadata"] = usage
    return json.dumps(chunk)


STREAM = (
    gemini_chunk("Hello "),
    gemini_chunk("there."),
    gemini_chunk(
        "",
        finish="STOP",
        usage={"promptTokenCount": 184, "candidatesTokenCount": 96, "totalTokenCount": 280},
    ),
)


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def route(seed_route, seed_price) -> int:
    seed_route(model="gemini-flash", provider="gemini", provider_model="gemini-2.0-flash")
    return seed_price(provider="gemini", provider_model="gemini-2.0-flash")


async def test_gemini_non_streamed_returns_the_openai_shape(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=RESPONSE)

    price_id = route(seed_route, seed_price)
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "gemini-flash"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "Hello there."}
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["prompt_tokens"] == 184
    assert response.headers["X-LGW-Provider"] == "gemini"
    assert seen["url"].endswith("/models/gemini-2.0-flash:generateContent")
    assert seen["payload"]["systemInstruction"] == {"parts": [{"text": "You are terse."}]}

    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "ok"
    assert row.provider == "gemini"
    assert row.price_id == price_id
    assert Decimal(row.cost_usd) == Decimal("0.000227")


async def test_gemini_streamed_returns_openai_chunks_with_reported_usage(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seen: dict = {}
    inner = sse_upstream(*STREAM)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return inner(request)

    route(seed_route, seed_price)
    upstream(handler)

    body = dict(BODY, stream=True)
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)

    assert response.status_code == 200
    assert seen["url"].endswith("/models/gemini-2.0-flash:streamGenerateContent?alt=sse")
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


async def test_gemini_usage_without_an_output_count_falls_back_to_the_estimate(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    """`usageMetadata` omits `candidatesTokenCount` until output exists.

    Reading the absent field as zero would bill nothing for generated output and record it as
    provider-reported, which is exactly the case `tokens_estimated` exists to expose.
    """
    route(seed_route, seed_price)
    upstream(
        sse_upstream(
            gemini_chunk("Hello ", usage={"promptTokenCount": 184, "totalTokenCount": 184}),
            gemini_chunk("there."),
            gemini_chunk("", finish="STOP"),
        )
    )

    body = dict(BODY, stream=True)
    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body)

    assert sse_payloads(response.text)[-1] == "[DONE]"
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
    assert row.status == "ok"
    assert row.prompt_tokens == 184
    assert row.tokens_estimated is True
    assert row.completion_tokens == 3  # ceil(len("Hello there.") / 4)
    assert Decimal(row.cost_usd) == Decimal("0.000078")


async def test_gemini_400_surfaces_the_upstream_message(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 400, "message": "bad request", "status": "INVALID_ARGUMENT"}},
        )

    route(seed_route, seed_price)
    upstream(handler)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=BODY)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "upstream_rejected"
    assert response.json()["error"]["message"] == "bad request"
