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

- 2026-07-29 - Phase 2 shipped in the 9 commits listed in `docs/phases.md`: the SSE relay
  (`app/services/streaming.py`) with usage capture, `include_usage` injection, ttfb, estimate
  fallback, mid-stream error events and client-disconnect accounting; the Anthropic and Gemini
  adapters with request, response, error, and stream translation; `GET /v1/models`; 76 new tests;
  and README provider and streaming sections. `stream: true` no longer returns 400.

## Verified on 2026-07-29

- `uv run pytest` 108 passed, `uv run ruff check .` clean, `uv run black --check .` clean.
- Live server on a fresh migrated database (`uvicorn app.main:app`): `GET /health` 200,
  `GET /v1/models` lists exactly the two models with active routes (the inactive route is
  absent, order is stable, shape matches `docs/api-contracts.md`), and `GET /v1/models` without
  a key returns 401.
- Live streamed request with a dummy `OPENAI_API_KEY` really reached `api.openai.com`, was
  rejected 401 before the first byte, and came back as an ordinary JSON `upstream_rejected`
  envelope carrying the upstream message; the audit row is `failed` with cost 0 and one
  `http_error` attempt. This is the pre-first-byte failover boundary working on the wire.
- Streaming behaviour under real uvicorn with a mock upstream (`httpx.MockTransport`, since no
  real provider key exists here): frames arrived one at a time 0.6 s apart, matching the
  upstream cadence exactly, so nothing is buffered; the row finalized `ok` with provider usage
  184/96, `tokens_estimated = false`, cost 0.000227, ttfb 1 ms, latency 4208 ms.
- `curl -N -m 2` killed mid-stream: the client got 4 frames, upstream was closed at once
  (`request.cut_off` logged 2.0 s in, not after the upstream's remaining 2.8 s of frames), the
  row is `cut_off` with `tokens_estimated = true` and cost 0.000007 committed, attempt `ok`.
- `stream_options.include_usage` live: the final usage chunk (empty `choices`, populated
  `usage`) appears only when the client asked; without it the same stream carries no usage
  chunk while the row still records exact provider usage.
- Logs from both live runs are JSON lines with request ids; grep for `lgw_` and for prompt text
  returned zero hits.
- CI is green on every Phase 2 commit pushed to `main` (lint, format check, 108 tests).

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

- Every checklist item that needs a real provider answer is unverified in this environment: no
  real `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY` exists here. Specifically not
  run: "concatenated content identical to the provider's own answer" for any provider, and the
  Anthropic and Gemini live routes (streamed and not). The network itself is reachable, so the
  gap is credentials, not connectivity: a streamed call did leave the process and was answered
  by OpenAI (401 on the dummy key). Everything else in those items is covered by
  `httpx.MockTransport` tests against the recorded wire shapes, and the streaming lifecycle was
  additionally exercised end to end under real uvicorn with a mock upstream.
- The live-provider checklist item from Phase 1 (real key through the OpenAI Python SDK) was not
  run for the same reason.
- CI on the first push failed on one test of mine, not on product code: two ULIDs minted inside
  the same millisecond differ only in their random suffix, so asserting they sort in creation
  order was wrong. The test now crosses a millisecond boundary before comparing, shipped as
  `fix(tests): order request ids across a millisecond boundary`. The workflow is green on that
  commit (lint, format check, 33 tests).

## Phase 2 defect review - 2026-07-29

A functionality-first review of the Phase 2 diff (`b44ea69..2ee55fc`) found three accounting
defects and one test that proved nothing. All four are fixed and pushed; `uv run pytest` is 112
passed with `ruff` and `black` clean after the last one.

- `fix(streaming): finalize the request when stream translation raises` (dd4fbad). Starlette
  awaits a response's `BackgroundTask` only when the body iterator ends without raising, and the
  relay caught just `ProviderFailure` and `httpx.HTTPError`. Anything else a translator can raise
  on a malformed payload (reproduced with an OpenAI usage chunk whose token counts are `null`,
  which makes `int(None)` raise `TypeError`) escaped the generator, so the background task never
  ran: the row stayed `in_flight` forever, no `attempts` row was written, the tokens already
  generated were never billed, and the upstream socket stayed open. This broke the invariant that
  every authenticated request ends `ok`, `failed`, or `cut_off`, and it contradicted the
  "malformed provider response -> failed attempt (`bad_response`)" row in `docs/architecture.md`.
  The relay now catches `Exception` last, records `bad_response`, and emits the documented SSE
  error event. `CancelledError` and `GeneratorExit` are `BaseException`, so a disconnected client
  still reaches the `cut_off` path untouched.
- `fix(providers): treat anthropic message_start output tokens as provisional` (97a74d5, formatted
  in 57cab0f). `message_start` carries the final `input_tokens` but only the `output_tokens`
  emitted so far, which is 1. The adapter reported both, so a stream that died or was cut off
  before `message_delta` was billed for one output token with `tokens_estimated = false`: 400
  characters of generated text cost 0.000075 instead of 0.000234. `docs/architecture.md` assigns
  input to `message_start` and output to `message_delta` for exactly this reason. Only the input
  count is reported now, so the `ceil(chars/4)` fallback applies and is flagged.
- `fix(providers): report gemini stream usage only when the chunk carries it` (8a4c53a). An absent
  `candidatesTokenCount` was read as zero, so a `usageMetadata` block that predates any output
  recorded zero completion tokens as provider-reported and billed the output as free. Both counts
  are now emitted only when the chunk actually carries them.
- `test(streaming): assert the restoration buffer's hold bound` (3ba6e2f). The old test pushed 200
  `x` characters, which can never begin a placeholder, so it passed for any hold limit including
  an unbounded one. The replacement feeds a partial placeholder from a pathological 112-character
  map entry and asserts the hold stays within `MAX_PLACEHOLDER_CHARS`; removing the `min(...)` cap
  makes it fail.

Reviewed and found sound: the reservation-free parts of the relay's ordering (usage events are
applied before the finish frame for all three providers), the `include_usage` split between what
is requested upstream and what is forwarded, pre-first-byte failure classification in
`open_stream`, the restoration buffer's prefix logic, and the absence of secrets or prompt text in
logs and error envelopes. Two known gaps were left alone as out of Phase 2 scope: `requests.provider`
is set at row creation rather than on the winning route (Phase 1 code, matters once failover
exists), and `X-LGW-Provider` / `X-LGW-Cache` are absent from error responses although
`docs/api-contracts.md` lists them on every completion response.

## Project status

- Phases 1 and 2 complete and pushed. Phase 3 (route table and CLI, failover, race-safe budgets)
  does not start until the owner approves Phase 2.
- Still carried from Phase 1: route resolution takes the first active `model_routes` row and
  there is no `lgw routes set`, so the README documents a direct SQL insert. Failover is Phase 3.

## Decisions log

- 2026-07-29 - Adapters emit a small `StreamEvent` (role, content, finish_reason, token counts)
  and `services/streaming.py` renders the OpenAI chunk frame. `docs/architecture.md` describes
  `translate_stream` as an iterator of canonical chunks; the chunk envelope only varies by
  request id, model, and created timestamp, all provider-independent, so building it in three
  adapters would have triplicated the framing rules that `docs/api-contracts.md` fixes. The
  serializer (`schemas.chunk_json`) is the single place that knows the wire shape.
- 2026-07-29 - Stream finalization runs in the response's `BackgroundTask`, not in the relay
  generator's `finally`. A client that disconnects leaves the generator suspended at a `yield`
  forever: Starlette cancels the sending task and the generator is only closed later by garbage
  collection, so a `finally` would commit cost late or not at all. The background task is the
  one point every ending passes through, and it runs after the cancel scope is absorbed, so it
  can still await the upstream close. Verified live: the row was `cut_off` two seconds in, while
  the mock upstream still had frames queued.
- 2026-07-29 - `stream_options: {"include_usage": true}` is always sent upstream to OpenAI and
  the synthetic usage chunk is forwarded to the client only when the client asked for it.
  Accounting must not depend on a client flag; the client contract must not change because of
  an internal need.
- 2026-07-29 - The streaming attempt row is written once, at stream end, with the outcome the
  stream actually reached (`ok` for a completed or client-cut-off stream, the classified failure
  for a mid-stream death). Cost still derives from exactly one attempt, because no failover is
  possible after the first client byte, so the no-double-billing invariant holds unchanged.
- 2026-07-29 - The placeholder restoration tail buffer landed with the relay even though
  `services/redaction.py` is Phase 4 work. `docs/architecture.md` files it under Streaming, it
  is a property of chunk boundaries rather than of detection, and Phase 4 would otherwise have
  to reopen the relay. It takes the map as an argument, which is empty everywhere in Phase 2, so
  the zero-redaction fast path (no buffering at all) is what actually runs today. Phase 4 only
  has to pass the real map.
- 2026-07-29 - The Anthropic and Gemini adapters were committed one commit before they were
  added to `PROVIDERS`. Splitting an adapter from its stream translation is only safe if the
  half-built adapter is unreachable; registering it late means no commit in the series can serve
  a request the adapter cannot finish.
- 2026-07-29 - Unknown stop reasons map to `null` on a non-streaming response but to `"stop"`
  inside a stream. A stream without a terminating chunk hangs SDK parsers, which is a worse
  failure than an imprecise reason on an unrecognised value.
- 2026-07-29 - Streaming log lines carry `request_id` in their `extra`. The request-id context
  variable is reset when the middleware returns, which happens before the body is streamed, so
  the relay and its background task would otherwise log without one.
- 2026-07-29 - The client-disconnect test drives the ASGI app with its own `receive`/`send`
  rather than going through `httpx.ASGITransport`, because that transport buffers the whole
  response body and can express neither incremental delivery nor a client walking away.

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
