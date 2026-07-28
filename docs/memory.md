# Project Memory - llm-gateway

Running log of what is done, in progress, and decided. Update after every meaningful chunk of
work; log every non-obvious decision with its reason. Keep entries short and dated.

## Completed

- 2026-07-27 - Planning documentation created (README, PRD, architecture, rules, phases, design,
  testing, api-contracts, launch-checklist, memory). No code yet; docs await owner review before
  Phase 1 starts.
- 2026-07-28 - Phase 1 shipped in the 15 commits listed in `docs/phases.md`: pinned pyproject plus
  `uv.lock`, `.env.example` and gitignore, settings, JSON logging with ULID request ids, the error
  envelope and handlers, the app factory with `GET /health`, all eight tables plus the initial
  Alembic migration, bearer-key auth, `lgw keys create|list|revoke|set-budget`, `lgw prices
  add|list` with the cost calculator, the OpenAI adapter, non-streaming
  `POST /v1/chat/completions` with attempt and audit rows, the CI workflow, 32 tests, and README
  run/test instructions.

## Verified on 2026-07-28

- `uv run alembic upgrade head` builds all eight tables plus `alembic_version` on a fresh file.
- `uv run uvicorn app.main:app` starts and `GET /health` returns `{"status":"ok"}` with an
  `X-LGW-Request-Id` header; uvicorn's own lines come out as JSON through our formatter.
- `lgw keys create` prints the raw key once on stderr; the row holds only hash and last4.
  `keys list` renders the documented table. `keys revoke live --yes` against a running server made
  the next request 401 with the documented envelope.
- `lgw prices add` and `prices list` produce the documented output and append-only rows.
- `uv run pytest` 32 passed, `uv run ruff check .` clean, `uv run black --check .` clean.
- Request logs are JSON lines carrying the request id and no prompt text, no raw keys (grepped the
  live server log for `lgw_`: zero hits).

## Not verified

- The live-provider checklist item (real `OPENAI_API_KEY` through the OpenAI Python SDK against
  all three of chat, error shape, and header) was not run: no real provider key is available in
  this environment, and the test suite is hermetic by design. The path is covered by
  `httpx.MockTransport` integration tests instead.
- GitHub Actions has not reported on the new workflow yet; CI status is unknown until the push
  lands.

## Project status

- Phase 1 complete and pushed. Phase 2 (streaming passthrough, Anthropic and Gemini adapters,
  `GET /v1/models`) does not start until the owner approves Phase 1.

## Decisions log

- 2026-07-28 - Database access is synchronous SQLAlchemy called from async handlers, with
  short-lived sessions that are never held across an upstream call. `docs/architecture.md` names
  SQLAlchemy and SQLite but no async driver, and adding `aiosqlite` would be a new dependency.
  SQLite is a local file, so the blocking window is microseconds; the async discipline rule is
  about network I/O. Revisit if a phase needs true DB concurrency.
- 2026-07-28 - ULID generation is a six-line function in `app/logging.py` (`new_request_id`)
  rather than a new dependency or a new utility module: request-id creation and request-id
  propagation belong together, and the rules cap the dependency set.
- 2026-07-28 - The request body limit (413 `payload_too_large`) landed in Phase 1 even though its
  commit is listed in Phase 5, because the Phase 1 verification checklist requires an oversize
  body to return its documented status. Phase 5 keeps the hardening work around it (pre-parse
  enforcement for streamed bodies, malformed-JSON edge cases).
- 2026-07-28 - `stream: true` is rejected with 400 `unsupported_parameter` until Phase 2 ships
  streaming. Serving a streamed request as a non-streamed body would break the client contract
  silently, which is worse than a precise error.
- 2026-07-28 - Route resolution in Phase 1 takes the first active `model_routes` row by position
  and requires a provider with an adapter; there is no `lgw routes set` yet (Phase 3), so the
  README documents a direct SQL insert for the single-route case. No failover logic was written
  early.
- 2026-07-28 - Unknown or forbidden request fields map to 400 `unsupported_parameter` and other
  schema violations to 400 `invalid_request`, matching `docs/api-contracts.md`. `tools`, `n`, and
  message `content` are declared on the schema so they produce the documented code instead of a
  generic extra-fields error.
- 2026-07-28 - A successful upstream call whose (provider, model) has no price row is finalized
  `ok` with `cost_usd = 0`, `price_id = null`, and a loud `prices.missing` error log, rather than
  failing a request the operator already paid for. Phase 3's startup validation is what makes this
  branch unreachable in practice.
- 2026-07-27 - Budget-exceeded returns 429 with `error.type: "insufficient_quota"` rather than
  402. OpenAI itself signals out-of-credit this way, so existing SDKs classify it as a
  non-retryable quota error instead of retry-looping against a hard budget cap, which a 402 or a
  plain 429 rate-limit type would invite.
- 2026-07-27 - The failover boundary is the first byte sent to the client, not the end of the
  upstream call. Before any client byte, every failover trigger (timeout, connect, 5xx, 429) is
  safe because the failed attempt produced no billable success and no client-visible output.
  After the first byte, a provider switch would emit duplicated or inconsistent content and could
  bill two providers for one request, so mid-stream failures terminate with an SSE error event
  and commit cost for tokens actually consumed.
- 2026-07-27 - Budgets use reservation accounting (estimate held by one conditional UPDATE,
  actual committed, remainder refunded) instead of check-then-write, because check-then-write has
  an unavoidable race window under concurrency. SQLite's single-writer model makes the
  conditional UPDATE atomic without SELECT FOR UPDATE, which SQLite lacks; this is a deliberate
  benefit of choosing SQLite. Stale holds are swept by TTL, and commit/release/sweep all guard on
  `state = 'held'` so exactly one transition wins.
- 2026-07-27 - The semantic cache embeds and stores only redacted content, and cached responses
  keep placeholders unrestored. A hit is discarded when the cached response references a
  placeholder absent from the current request's map. Consequence: no PII at rest in the cache and
  no possibility of restoring one request's PII into another request's response, at the cost of
  slightly lower hit rates on PII-heavy prompts. Cache entries are additionally scoped to
  (key, model) so entries never cross tenants.
- 2026-07-27 - Token counts always prefer provider-reported usage, including in streams (OpenAI
  `include_usage` injected upstream, Anthropic `message_start`/`message_delta`, Gemini
  `usageMetadata`), rather than shipping a local tokenizer. tiktoken only matches OpenAI models,
  so local counting would be wrong for two of three providers while adding a dependency; the
  `ceil(chars/4)` fallback is used only when a stream dies before usage arrives and is flagged
  with `tokens_estimated` so reports stay honest.
