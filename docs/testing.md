# Testing - llm-gateway

## Strategy

- **Hermetic by default.** pytest never touches a live provider, Redis, or the network. Upstreams
  are `httpx.MockTransport` fixtures replaying recorded wire shapes (OpenAI, Anthropic, Gemini,
  success and failure, streamed and not); the embedding provider is a deterministic fake that
  maps known strings to fixed vectors so similarity thresholds are testable exactly.
- **pytest + pytest-asyncio.** The app is exercised through `httpx.AsyncClient` over ASGI
  transport (real middleware, real streaming), services through direct calls. Each test gets a
  fresh SQLite database (tmp file, WAL, migrations applied once per session and copied) so
  concurrency tests run against the real engine, not in-memory shortcuts.
- **Money and privacy paths get adversarial tests**, not happy-path tests: races, crashes between
  steps, malformed upstream bodies, disconnects mid-stream.

## What gets unit coverage

- Redaction: each detector (email, phone formats, Luhn accept/reject), stable numbering, repeated
  values, restoration, unknown-placeholder passthrough, the streaming tail buffer against
  placeholder splits at every boundary offset.
- Cost: price resolution by `effective_at` (before/on/after boundaries), cost math, the token
  estimator.
- Budget SQL: reserve accept/reject at the exact boundary, commit/release/sweep single-winner
  behavior under interleaving, overshoot bounded by one estimate.
- Translators: request/response/stream/error mapping per provider against recorded fixtures;
  finish_reason and usage field mapping; unsupported-param rejection.
- Cache: hash canonicalization, cosine math, threshold and scan-limit behavior,
  placeholder-mismatch miss rule, eviction predicates.

## What gets integration coverage

- Full chat flows per provider: non-stream, stream, SDK-shaped assertions on the response.
- Failover matrix: 500, timeout, connect error, 429, provider 400 (no failover), exhaustion
  mapping, attempts rows, single `ok` attempt, cost from the winning attempt only.
- Budget under concurrency: `asyncio.gather` of N requests against a budget that affords fewer;
  assert admitted count, 429s, and `spent + reserved` cap invariant sampled during the run.
- Streaming lifecycle: usage captured from stream, `include_usage` forwarding rule, client
  disconnect -> `cut_off` with committed cost, upstream mid-stream death -> SSE error event.
- Cache tiers end to end: exact hit, semantic hit, scoping across keys, Redis-down degradation
  (fake redis client that raises), streamed hit replay.
- Auth and errors: every documented error code produced by at least one test; envelope shape
  asserted everywhere.
- CLI: commands run via their entry function against the test DB; table and `--json` output
  snapshots; exit codes 0/1/2; `usage --verify` catches a hand-corrupted `spent_usd`.

## End-to-end (manual)

Checks that need real provider keys or a real client run by hand against a live server, never in
CI:

- The official OpenAI Python SDK, with only `base_url` and `api_key` changed, completes a chat
  and a streamed chat on each configured provider; the request row has provider-reported tokens,
  a non-zero cost, and a `price_id`.
- `curl -N` on a streamed request shows frames arriving at the upstream's cadence. Killing curl
  mid-stream closes the upstream promptly and leaves the row `cut_off` with cost committed.
- The final usage chunk appears only when the client sent `stream_options.include_usage`.
- With route 1 forced to fail, the request succeeds via route 2 and the audit shows both
  attempts, exactly one of them `ok`.
- A revoked key gets 401 on the next request.
- The server log is JSON lines with request ids; grepping it for `lgw_` and for prompt text
  returns nothing.

## Exact commands

```bash
uv run pytest                       # full suite
uv run pytest tests/unit           # units only
uv run pytest -k budget            # by keyword
uv run ruff check .                 # lint (must be clean)
uv run black --check .              # formatting (must be clean)
```

First-time setup:

```bash
uv sync
cp .env.example .env                # fill provider keys for manual testing only
uv run alembic upgrade head
uv run python -m app.cli keys create --name dev
uv run uvicorn app.main:app
```

## CI

GitHub Actions on push and PR to `main`. The check job runs on Python 3.12 and 3.13:
`uv sync --frozen`, then `ruff check .`, `black --check .`, and `pytest` with a 90 percent
coverage gate. A second job builds the Docker image, starts it, waits for `GET /health`, and runs
the CLI inside the container. No secrets in CI; the suite is hermetic by design and must stay
that way (a test that needs a real key is a defect).

## Definition of done for a change

1. `uv run ruff check .` and `uv run black --check .` clean.
2. `uv run pytest` green, with new tests in the same change.
3. Behaviour that only a live provider can show is checked by hand as described above.
