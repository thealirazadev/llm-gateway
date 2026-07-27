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

## End-to-end (manual, per phase)

The per-phase verification checklists in `docs/phases.md` are the e2e layer: real provider keys,
the real OpenAI SDK, curl for SSE and disconnects, a real Redis start/stop. These are run at phase
boundaries, not in CI.

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

## CI plan

GitHub Actions on push and PR to `main`: one job, Python 3.12, `uv sync --frozen`, then
`ruff check .`, `black --check .`, `pytest`. No secrets in CI; the suite is hermetic by design and
must stay that way (a test that needs a real key is a defect). CI green is a merge requirement
from Phase 1 onward.

## Definition of done for a feature

1. `uv run ruff check .` and `uv run black --check .` clean.
2. `uv run pytest` green, new tests included in the same commit series.
3. The feature's items in the current phase's verification checklist pass.
