import json
import logging

import httpx

from app.db import session_scope
from app.models import Request as RequestRow
from tests.conftest import (
    openai_chunk,
    openai_response,
    openai_usage_chunk,
    sse_payloads,
    sse_upstream,
)

PROMPT = (
    "I'm ana@example.com, call +1 415-555-0132 or (020) 7946 0958. "
    "Card 4111 1111 1111 1111, not 4111 1111 1111 1112."
)
ORIGINALS = ["ana@example.com", "415-555-0132", "7946 0958", "4111 1111 1111 1111"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def body(stream: bool = False) -> dict:
    return {
        "model": "gpt-4.1-mini",
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": stream,
    }


async def test_upstream_sees_placeholders_and_the_client_gets_the_originals(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream, caplog
) -> None:
    seed_route()
    seed_price()
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        return httpx.Response(
            200, json=openai_response("Noted <pii_email_1> and <pii_card_1>, not <pii_card_2>.")
        )

    upstream(handler)
    caplog.set_level(logging.DEBUG)

    response = await client.post("/v1/chat/completions", headers=auth(api_key), json=body())

    assert response.status_code == 200
    upstream_prompt = json.loads(sent[0])["messages"][0]["content"]
    assert upstream_prompt == (
        "I'm <pii_email_1>, call <pii_phone_1> or <pii_phone_2>. "
        "Card <pii_card_1>, not 4111 1111 1111 1112."
    )
    assert response.json()["choices"][0]["message"]["content"] == (
        "Noted ana@example.com and 4111 1111 1111 1111, not <pii_card_2>."
    )
    with session_scope() as session:
        row = session.get(RequestRow, response.headers["X-LGW-Request-Id"])
        stored = json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})
    assert row.redactions == 4
    for value in ORIGINALS:
        assert value not in stored
        assert value not in caplog.text


async def test_a_placeholder_split_across_chunks_is_restored_in_the_stream(
    client: httpx.AsyncClient, api_key: str, seed_route, seed_price, upstream
) -> None:
    seed_route()
    seed_price()
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": ""}),
            openai_chunk({"content": "Mail <pii_em"}),
            openai_chunk({"content": "ail_1> today."}),
            openai_chunk({}, finish_reason="stop"),
            openai_usage_chunk(),
            "[DONE]",
        )
    )

    response = await client.post(
        "/v1/chat/completions", headers=auth(api_key), json=body(stream=True)
    )

    text = ""
    for payload in sse_payloads(response.text):
        if payload != "[DONE]":
            for choice in json.loads(payload).get("choices", []):
                text += choice["delta"].get("content") or ""
    assert text == "Mail ana@example.com today."
