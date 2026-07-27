# Architecture - llm-gateway

## App flow

```
Client (OpenAI SDK or raw HTTP, Authorization: Bearer lgw_...)
        |  POST /v1/chat/completions
        v
Request pipeline (FastAPI, async)
        |- authenticate key          -> unknown/revoked: 401, nothing recorded
        |- validate body             -> unsupported params / unknown model: 4xx
        |- create requests row       -> status in_flight (ULID request id)
        |- redact PII                -> placeholders substituted, map in memory only
        |- reserve budget            -> one conditional UPDATE; over budget: 429
        |- cache lookup (opt-in)     -> hit: release reservation, restore PII, respond
        |- attempt loop over routes  -> translate to provider wire format, call via httpx
        |     timeout/connect/5xx/429 -> record attempt, next route; other 4xx -> stop
        |- non-stream: usage -> cost -> commit -> restore PII -> cache store -> respond
        |- stream: proxy translated SSE chunks (PII tail buffer), capture usage
        |     from the stream, commit on completion or disconnect
        v
SQLite (WAL): keys, routes, prices, budget_periods, reservations, requests, attempts, cache
Redis (optional): exact-cache accelerator only, never source of truth
Background: stale-reservation sweeper, cache TTL eviction
Admin CLI (lgw): keys / routes / prices / usage, direct DB access, no HTTP
```

## Request lifecycle (non-streaming)

1. **Auth.** SHA-256 the bearer token, look up an active `virtual_keys` row by `key_hash` (no
   secret comparison, so no timing channel). Miss or revoked: 401 `invalid_api_key`.
2. **Validate.** Pydantic (`extra="forbid"`) over the OpenAI chat shape: `model`, `messages`
   (string content), `stream`, `stream_options`, `temperature`, `top_p`, `max_tokens`, `stop`.
   `tools`, image parts, or `n > 1`: 400 `unsupported_parameter`; unknown model: 404.
3. **Record.** Insert the `requests` row with status `in_flight` and a ULID id: crashes stay
   forensically visible and `reservations` gets a valid FK target.
4. **Redact.** Replace emails, phones, and Luhn-valid card numbers with numbered placeholders;
   keep the map in request memory only (see "PII redaction").
5. **Reserve budget.** Estimate cost as `est_input_tokens * max_input_price + (max_tokens or
   LGW_DEFAULT_MAX_OUTPUT_TOKENS) * max_output_price`, max prices taken across the model's
   candidate routes so the estimate covers whichever route wins. Reserve atomically; rejection:
   429 `budget_exceeded`. Null-budget keys skip this step.
6. **Cache** (opt-in per key): exact-match lookup by prompt hash, then semantic lookup by
   embedding similarity. Hit: release the reservation, restore PII, record `cache_hit`, respond.
7. **Attempt loop.** For each active route in priority order, translate the redacted request and
   call with the configured connect/read timeouts. One attempt per route, no per-route retries
   (the client owns end-to-end retries); every attempt writes an `attempts` row. Failover
   triggers: timeout, connect error, 5xx, 429. Any other 4xx means the request is bad for that
   provider: translate the error and return without failover.
8. **Success.** Parse provider-reported usage, resolve the price row, compute cost, commit the
   reservation, restore PII, store a cache entry when eligible, finalize the row (`ok`, tokens,
   cost, price_id, latency), respond with `X-LGW-Request-Id`/`X-LGW-Provider`/`X-LGW-Cache`.
9. **Exhaustion.** Release the reservation, mark the row `failed`, return 502 `upstream_error`,
   or 429 `upstream_rate_limited` when every attempt was a 429.

## Streaming (SSE passthrough with accounting)

- **Failover boundary.** The upstream response status is checked before anything is sent to the
  client, so pre-first-byte errors fail over exactly like the non-streaming path. Once the first
  translated chunk is written to the client the attempt is committed: a later upstream error
  terminates the stream with a final SSE error event, cost for tokens already generated is
  committed, and the row is marked `failed`. No mid-stream provider switch: it would duplicate
  content and double-bill.
- **Usage capture.** All three providers report usage inside the stream: OpenAI via injected
  `stream_options: {"include_usage": true}` (the synthetic usage chunk is forwarded only if the
  client asked for it), Anthropic via `message_start` (input) and `message_delta` (output),
  Gemini via `usageMetadata` on chunks. A stream that ends without usage falls back to
  `ceil(chars / 4)` over redacted prompt plus streamed content, flagged `tokens_estimated`.
- **Client disconnect** (write failure or cancelled task): close upstream immediately, commit
  usage observed so far (or the estimate), finalize with status `cut_off`. Consumed tokens are
  never refunded; the provider billed for them.
- **PII restoration in-stream.** Deltas pass through a restoration buffer that holds back the
  longest emitted suffix that could be a placeholder prefix (bounded by the longest placeholder,
  under 24 chars), so a placeholder split across upstream chunks is restored before flushing.
  Zero-redaction requests bypass the buffer: the common path adds no latency.
- **Cache hits under `stream: true`** replay as role chunk, one content chunk, finish chunk,
  `[DONE]`. `ttfb_ms` is recorded at the first upstream chunk, total latency at stream end.

## Budget accounting (race-safe reservation)

State lives in `budget_periods` (one row per key per UTC calendar month, created lazily with
`INSERT OR IGNORE`) and `reservations` (one row per reserving request).

- **Reserve** is one conditional statement, atomic under SQLite's single-writer model:
  ```sql
  UPDATE budget_periods
     SET reserved_usd = reserved_usd + :est
   WHERE key_id = :k AND period = :p
     AND spent_usd + reserved_usd + :est <= :limit
  ```
  Zero rows updated means rejection. `:limit` is read from `virtual_keys.monthly_budget_usd` at
  request time, so budget changes apply immediately, mid-month.
- **Commit** (success): transition the reservation `held -> committed` via
  `UPDATE ... WHERE state = 'held'`; only the winner applies `spent_usd += actual_cost,
  reserved_usd -= held_amount`. Actual cost may exceed the estimate (long completion); the
  overshoot is bounded by one request.
- **Release** (failure or cache hit): transition `held -> released`; the winner applies
  `reserved_usd -= held_amount`.
- **Sweeper.** Every 60 seconds, release reservations still `held` past `expires_at`
  (`LGW_RESERVATION_TTL_SECONDS`, deliberately longer than any allowed stream) and mark their
  `in_flight` rows `failed(swept)`. Commit, release, and sweep all guard on `state = 'held'`, so
  a race resolves to exactly one winner. If a sweep beats a late commit, the commit still adds
  actual cost to `spent_usd` but skips the `reserved_usd` decrement the sweep already made:
  spend is never lost, reserved never goes negative.
- **Reconciliation invariant** (`lgw usage --verify`, also tested): per key and month,
  `spent_usd == SUM(requests.cost_usd)`, and `reserved_usd == 0` when nothing is in flight.

## Provider translation

The OpenAI chat format is canonical: the client contract and the internal representation.
Adapters implement one interface: `translate_request`, `parse_response`, `translate_stream`
(async iterator of canonical chunks), `translate_error`. OpenAI's adapter is a passthrough.

| Concern | Anthropic (Messages API) | Gemini (generateContent) |
|---|---|---|
| System message | Hoisted to top-level `system` | Hoisted to `systemInstruction` |
| Roles | `user`/`assistant` as-is | `assistant` -> `model` |
| `max_tokens` | Required: default `LGW_DEFAULT_MAX_OUTPUT_TOKENS` when absent | `generationConfig.maxOutputTokens` |
| Sampling | `temperature`, `top_p`, `stop_sequences` | `generationConfig` equivalents |
| Finish reason | `end_turn` -> `stop`, `max_tokens` -> `length` | `STOP` -> `stop`, `MAX_TOKENS` -> `length`, safety block -> `content_filter` |
| Usage | `usage.input_tokens` / `output_tokens` | `usageMetadata.promptTokenCount` / `candidatesTokenCount` |
| Streaming | `message_start` / `content_block_delta` / `message_delta` / `message_stop` -> OpenAI chunks | `streamGenerateContent?alt=sse` chunks -> OpenAI chunks |
| Errors | `error.type` / `message` -> envelope | `error.status` / `message` -> envelope |

Unsupported params are dropped; anything not representable canonically is rejected at validation.

## Price table versioning

`prices` is append-only. The price for a request is the row with the greatest
`effective_at <= now` for its (provider, provider_model); rows are never updated or deleted and
the CLI offers no mutation. Each request stores both `cost_usd` and the `price_id` used, so
history is immutable by construction. Startup validation refuses to serve a model whose routes
lack a price row: an un-costable request cannot occur at runtime.

## PII redaction

- **Detectors**, one pass per message text: email (RFC-lite regex), phone (E.164 and common
  separator formats, minimum 7 digits), card numbers (13-19 digits with optional spaces/hyphens,
  accepted only when Luhn-valid, which removes most false positives such as order ids).
- **Placeholders** are `<pii_email_N>`, `<pii_phone_N>`, `<pii_card_N>`, numbered by first
  appearance; identical values reuse their placeholder so the model sees referential consistency.
- **The map never leaves the process**: not persisted, not logged, dropped at request end.
  Redaction runs before budget estimation, embedding, and any upstream call, so raw PII never
  leaves the gateway in any form.
- **Restoration** replaces only placeholders present in this request's map; placeholder-shaped
  text the model invents passes through untouched, never substituted with other data.
- Redaction is always on; only the count is recorded (`requests.redactions`).

## Semantic cache (opt-in per key)

Two layers, both scoped to `(key_id, model)` so entries can never leak across tenants:

1. **Exact layer.** Key: SHA-256 over canonical JSON of redacted messages, model, and sampling
   params. SQLite `cache_entries` is the source of truth; with `REDIS_URL` set, Redis mirrors
   this layer as a TTL accelerator, and any Redis error degrades silently (warning) to SQLite.
2. **Semantic layer.** The redacted prompt is embedded via `LGW_EMBEDDING_PROVIDER` /
   `LGW_EMBEDDING_MODEL`; the gateway scans the newest `LGW_CACHE_SCAN_LIMIT` entries for the
   key/model with pure-Python cosine similarity (no numpy; acceptable at the default bound of
   512) and serves the best match at or above `LGW_CACHE_SIMILARITY_THRESHOLD`. No embedding
   provider configured: this layer is off, the exact layer still works.

Entries hold the provider response **with placeholders intact** (never restored PII) plus token
counts so hits report spec-shaped `usage`. A semantic hit whose cached response references a
placeholder absent from the current request's map is discarded as a miss: restoration must never
fabricate values. Only successful, non-cut-off responses are cached; entries expire after
`LGW_CACHE_TTL_HOURS` and are capped per key (oldest evicted), pruned by a background task.

## Failure modes

| Failure | Handling |
|---|---|
| Provider timeout / connect / 5xx / 429 | Attempt recorded, failover to next route; exhausted -> 502 `upstream_error` (or 429 when all attempts were 429); reservation released. |
| Provider non-429 4xx | No failover; translated envelope with upstream status; reservation released. |
| Malformed provider response | Failed attempt (`bad_response`), fails over; body excerpt logged (capped), never returned. |
| Upstream error mid-stream (after first client byte) | SSE error event, stream closed, observed cost committed, row `failed`, no failover. |
| Client disconnect mid-stream | Upstream closed, observed/estimated usage committed, row `cut_off`. |
| Stream ends without usage | `ceil(chars/4)` estimate, `tokens_estimated = true`, cost committed from it. |
| First request of a month (period row missing) | `INSERT OR IGNORE` then reserve; the unique index makes the concurrent race harmless. |
| Crash between reserve and commit | Sweeper releases after TTL, row `failed(swept)`; budget frees itself without operator action. |
| Redis down / embedding call fails | Warning logged; exact cache served from SQLite / semantic layer skipped for that request; no request ever fails because of the cache. |
| Audit write fails at finalize | The response already sent is not withdrawn; loud log with request id. Budget commit and audit finalize share one transaction, so money and audit cannot diverge. |
| No price row for a routed model | Refused at startup and by `lgw routes set`; cannot occur at request time. |
| SQLite busy under concurrency | WAL mode, `busy_timeout` 5000 ms, short write transactions; contention shows as latency, not errors. |

## Correctness invariants

- A reservation transitions exactly once, `held -> committed` or `held -> released`, enforced by
  conditional UPDATE on `state`; commit, release, and sweep race safely to one winner.
- At most one attempt per request is `ok`; `cost_usd` derives only from that attempt's usage and
  exactly one price row. Failed attempts never contribute cost: failover cannot double-bill.
- No failover and no retry after the first response byte reaches the client.
- Per key and month, `spent_usd` equals summed `requests.cost_usd`, and `reserved_usd` returns
  to zero when idle (reconciled by `lgw usage --verify`).
- `prices` rows are immutable; every costed request references the price row it used.
- The placeholder map is process-memory only; no unredacted prompt, response, or PII value is
  written to the database, logs, Redis, or the embedding API.
- Cache entries are read and written only under the requesting key's `(key_id, model)` scope.
- Every authenticated request has exactly one `requests` row ending `ok`, `failed`, or `cut_off`.

## Tech stack with rationale

- **Python 3.12 + FastAPI** - Async-first suits an I/O-bound proxy; Pydantic strictly validates
  the OpenAI wire shape; `StreamingResponse` covers SSE. Versions pinned, `uv.lock` committed.
- **SQLAlchemy 2.x + SQLite (WAL) + Alembic** - One file holds config, budgets, audit, and
  cache: zero extra infrastructure, one backup. Single-writer SQLite makes the one-statement
  budget reservation trivially atomic; the throughput ceiling is acceptable and documented.
- **httpx** - Async client with per-request timeouts, native response streaming, and hermetic
  tests via `MockTransport`. No provider SDKs: translation stays explicit and testable.
- **redis-py (optional import)** - Loaded only when `REDIS_URL` is set; the default install runs
  with zero non-Python infrastructure.
- **pytest + pytest-asyncio, uv, ruff, black** - Hermetic async tests; locking, linting, and
  formatting matching the sibling Python projects.

## Data model

Table and column names are the contract; the coding agent must not rename them.

### virtual_keys
| Field | Type | Notes |
|---|---|---|
| id | integer PK | |
| name | string, unique | CLI handle, e.g. "billing-app" |
| key_hash | string(64), unique | SHA-256 hex of the raw key; raw key never stored |
| key_last4 | string(4) | display only |
| monthly_budget_usd | numeric(12,2), nullable | null means unlimited |
| cache_enabled / active | boolean | defaults false / true |
| created_at / revoked_at | datetime / nullable | |

### model_routes
| Field | Type | Notes |
|---|---|---|
| id | integer PK | |
| model | string | public model name clients send |
| position | integer | priority, 1 = first |
| provider / provider_model | string enum / string | `openai` \| `anthropic` \| `gemini` / upstream model id |
| active | boolean, default true | |

Indexes: unique `(model, position)`; `model`.

### prices
| Field | Type | Notes |
|---|---|---|
| id | integer PK | |
| provider / provider_model | string | |
| input_usd_per_mtok / output_usd_per_mtok | numeric(12,6) | USD per million tokens |
| effective_at / created_at | datetime | resolution: max `effective_at <= now`; append-only |

Index: `(provider, provider_model, effective_at)`.

### budget_periods
| Field | Type | Notes |
|---|---|---|
| id | integer PK | |
| key_id | FK -> virtual_keys | |
| period | string(7) | UTC month, e.g. `2026-07` |
| spent_usd / reserved_usd | numeric(12,6), default 0 | committed actuals / in-flight estimates |

Index: unique `(key_id, period)`.

### reservations
| Field | Type | Notes |
|---|---|---|
| id | integer PK | |
| request_id | string(26) FK -> requests, unique | at most one per request |
| period_id | FK -> budget_periods | |
| amount_usd | numeric(12,6) | the held estimate |
| state | string enum | `held` \| `committed` \| `released` |
| created_at / expires_at | datetime | sweeper releases past `expires_at` |

Index: `(state, expires_at)` for the sweeper.

### requests
| Field | Type | Notes |
|---|---|---|
| id | string(26) PK | ULID; returned as `X-LGW-Request-Id` |
| key_id | FK -> virtual_keys | indexed |
| model | string | as requested by the client |
| provider / provider_model | string, nullable | winning route; null on cache hit or total failure |
| status | string enum | `in_flight` \| `ok` \| `failed` \| `cut_off` |
| streamed / cache_hit | boolean | |
| prompt_tokens / completion_tokens | integer, nullable | |
| tokens_estimated | boolean, default false | true when usage was estimated |
| cost_usd | numeric(12,6), default 0 | zero for cache hits and failures |
| price_id | FK -> prices, nullable | the exact price row used |
| latency_ms / ttfb_ms | integer, nullable | ttfb only for streams |
| redactions | integer, default 0 | count only, never values |
| error_code | string, nullable | e.g. `upstream_error`, `swept` |
| created_at | datetime | finalized once; no updated_at |

Indexes: `(key_id, created_at)`, `created_at`, `status`. No prompt or response bodies, ever.

### attempts
| Field | Type | Notes |
|---|---|---|
| id | integer PK | |
| request_id | FK -> requests | indexed, cascade on delete |
| position | integer | order tried |
| provider / provider_model | string | |
| outcome | string enum | `ok` \| `http_error` \| `rate_limited` \| `timeout` \| `connect_error` \| `bad_response` |
| status_code / latency_ms | smallint, nullable / integer | |
| created_at | datetime | immutable rows |

### cache_entries
| Field | Type | Notes |
|---|---|---|
| id | integer PK | |
| key_id / model | FK / string | scope |
| prompt_hash | string(64) | SHA-256 of redacted messages + model + sampling params |
| embedding | blob, nullable | float32 array; null when semantic layer disabled |
| response_json | text | OpenAI-format response, placeholders intact |
| prompt_tokens / completion_tokens | integer | reported in hit responses |
| hit_count / last_hit_at / created_at / expires_at | mixed | stats and TTL eviction |

Indexes: unique `(key_id, model, prompt_hash)`; `(key_id, model, created_at)`.

## Directory layout

```
llm-gateway/
|- pyproject.toml / uv.lock / .env.example / alembic.ini
|- migrations/versions/           # one migration per schema change, never edited after apply
|- app/
|  |- main.py                     # app factory, startup validation, background tasks
|  |- config.py                   # env-backed settings, read once
|  |- logging.py                  # structured JSON logging, request ids
|  |- errors.py                   # envelope types + exception handlers
|  |- auth.py                     # bearer parsing, key hashing dependency
|  |- db.py                       # engine, session, WAL pragmas
|  |- models.py                   # SQLAlchemy models (tables above)
|  |- schemas.py                  # Pydantic: canonical request/response/chunk shapes
|  |- routes/                     # chat.py, models.py, health.py
|  |- providers/                  # base.py (interface, shared client), openai.py,
|  |                              #   anthropic.py, gemini.py
|  |- services/                   # router.py (failover), budget.py, cost.py, redaction.py,
|  |                              #   cache.py, streaming.py
|  |- cli.py                      # lgw entry point (argparse), direct DB access
|- tests/unit/                    # redaction, cost, budget sql, cosine, translators
|- tests/integration/             # endpoint flows with httpx.MockTransport upstreams
|- docs/
```

## External dependencies and required env vars

Runtime services: none required beyond the provider APIs the operator routes to. Redis is
optional. Variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLite path, e.g. `sqlite:///data/gateway.db`. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | Upstream credentials; required only for providers present in routes. |
| `REDIS_URL` | Optional; enables the exact-cache accelerator. |
| `LGW_EMBEDDING_PROVIDER` / `LGW_EMBEDDING_MODEL` | Optional; enables the semantic cache layer. |
| `LGW_CONNECT_TIMEOUT_SECONDS` / `LGW_UPSTREAM_TIMEOUT_SECONDS` | Upstream connect timeout (default 5); read timeout, non-stream (default 120), per-chunk gap for streams (default 60). |
| `LGW_DEFAULT_MAX_OUTPUT_TOKENS` | Output cap for estimates and Anthropic's required max_tokens (default 1024). |
| `LGW_RESERVATION_TTL_SECONDS` | Sweeper threshold (default 900). |
| `LGW_CACHE_SIMILARITY_THRESHOLD` / `LGW_CACHE_TTL_HOURS` / `LGW_CACHE_SCAN_LIMIT` / `LGW_CACHE_MAX_ENTRIES_PER_KEY` | Cache tuning (defaults 0.93 / 24 / 512 / 2000). |
| `LGW_MAX_BODY_KB` | Request body limit (default 256); larger bodies get 413. |
| `LGW_LOG_BODIES` | Default false; true logs redacted bodies to the structured log only, never the database. |

Config is read once in `app/config.py`; code reads settings, never `os.environ` directly.
