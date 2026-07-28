"""Client-disconnect behaviour, driven over raw ASGI.

httpx's ASGITransport buffers the whole response body, so it cannot express a client that
walks away mid-stream. Calling the app directly with our own receive/send is the only
hermetic way to assert both that chunks are sent incrementally and that a cut-off stream is
still accounted for.
"""

import asyncio
import json
from decimal import Decimal

from sqlalchemy import select

from app.db import session_scope
from app.models import Attempt
from app.models import Request as RequestRow
from tests.conftest import openai_chunk, openai_usage_chunk, sse_upstream

BODY = {
    "model": "gpt-4.1-mini",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": True,
}


async def drive(app, api_key: str, body: dict, disconnect_after: int | None) -> list[dict]:
    """Run one request against the ASGI app, optionally disconnecting after N body chunks."""
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gateway.test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
            (b"authorization", f"Bearer {api_key}".encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("gateway.test", 80),
    }
    disconnected = asyncio.Event()
    body_sent = False
    messages: list[dict] = []

    async def receive() -> dict:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        chunks = sum(1 for m in messages if m["type"] == "http.response.body" and m.get("body"))
        if disconnect_after is not None and chunks >= disconnect_after:
            disconnected.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return messages


def body_chunks(messages: list[dict]) -> list[bytes]:
    return [m["body"] for m in messages if m["type"] == "http.response.body" and m.get("body")]


async def test_chunks_reach_the_client_one_frame_at_a_time(
    db, api_key: str, seed_route, seed_price, upstream
) -> None:
    from app.main import create_app

    seed_route()
    seed_price()
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": ""}),
            openai_chunk({"content": "one "}),
            openai_chunk({"content": "two"}),
            openai_chunk({}, finish_reason="stop"),
            openai_usage_chunk(),
            "[DONE]",
        )
    )

    messages = await drive(create_app(), api_key, BODY, disconnect_after=None)

    # Role, two content deltas, the finish chunk, and [DONE]: never one buffered blob.
    assert len(body_chunks(messages)) == 5
    assert body_chunks(messages)[-1] == b"data: [DONE]\n\n"


async def test_client_disconnect_commits_the_observed_cost_as_cut_off(
    db, api_key: str, seed_route, seed_price, upstream
) -> None:
    from app.main import create_app

    seed_route()
    seed_price()
    upstream(
        sse_upstream(
            openai_chunk({"role": "assistant", "content": ""}),
            openai_chunk({"content": "half an "}),
            openai_chunk({"content": "answer"}),
            stall=5,
        )
    )

    messages = await drive(create_app(), api_key, BODY, disconnect_after=2)

    assert len(body_chunks(messages)) >= 2
    with session_scope() as session:
        row = session.scalar(select(RequestRow))
        attempt = session.scalar(select(Attempt))
    assert row.status == "cut_off"
    assert row.error_code is None
    assert row.tokens_estimated is True
    assert row.completion_tokens >= 1
    assert Decimal(row.cost_usd) > Decimal("0")
    assert attempt.outcome == "ok"
