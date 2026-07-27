# Phases - llm-gateway

Rule: phase N+1 does not start until the owner approves phase N. Within a phase, commit in the
listed order: one commit per feature/task, Conventional Commits, each commit leaves the tree
working (lint, format, tests green).

Ordering rationale: Phase 1 ships an end-to-end billable passthrough (auth, cost, audit) so every
later feature lands on real accounting. Phase 2 adds the two hardest wire problems (streaming
accounting, provider translation) before anything depends on their shape. Phase 3 adds the
money-safety layer (failover without double-billing, race-safe budgets). Phase 4 adds the privacy
and cache layer. Phase 5 hardens for launch. The senior differentiators (usage-accurate streaming,
atomic reservations, single-winner state transitions) are load-bearing and may not slip.

---

## Phase 1 - Foundation and billable OpenAI passthrough

**Goal**: a client with a virtual key gets a non-streaming chat completion through the OpenAI
provider, and the gateway records exact tokens and cost from the versioned price table in the
audit log. Includes config, structured logging, the error envelope, migrations, the CLI's key and
price management, and base CI.

### Tasks

- Project scaffold: pyproject with pinned deps, uv lock, ruff and black config, gitignore,
  `.env.example` with every variable from `docs/architecture.md`.
- `app/config.py` settings, `app/logging.py` JSON logs with request ids, `app/errors.py`
  envelope and handlers, app factory with `GET /health`.
- SQLAlchemy models and the initial Alembic migration for all eight tables.
- Bearer auth dependency (SHA-256 lookup, active check); `lgw keys create|list|revoke` and
  `lgw prices add|list`; cost calculator with `effective_at` resolution and the token estimator.
- OpenAI provider adapter (passthrough) and the non-streaming `POST /v1/chat/completions` flow:
  in_flight row, single attempt, usage parse, cost, finalize, response headers.
- CI workflow: ruff check, black check, pytest.

### Expected commits

1. `build: scaffold project with pyproject, uv lock, ruff and black config`
2. `chore: add env example and gitignore`
3. `feat(config): add settings module reading environment variables`
4. `feat(logging): add structured json logging with request ids`
5. `feat(errors): add openai compatible error envelope and handlers`
6. `feat(app): create fastapi app with health route`
7. `feat(db): add sqlalchemy models and initial migration`
8. `feat(auth): add virtual key bearer authentication`
9. `feat(cli): add key management commands`
10. `feat(prices): add versioned price table with cli and cost calculator`
11. `feat(providers): add openai provider adapter`
12. `feat(chat): add non-streaming chat completions with audit row`
13. `build(ci): add workflow running lint, format check, and tests`
14. `test(phase1): cover auth, cost math, and the passthrough flow`
15. `docs: add run and test instructions to readme`

### Verification checklist

- [ ] `uv run alembic upgrade head` creates the full schema on a fresh file; `uv run uvicorn
      app.main:app` starts; `GET /health` returns `{"status":"ok"}`.
- [ ] `lgw keys create --name demo` prints a `lgw_` key once; the DB row holds only hash and
      last4; `lgw keys list` shows it; `lgw keys revoke demo` makes the next request 401.
- [ ] With a real `OPENAI_API_KEY`, a chat completion via the OpenAI Python SDK (base_url swapped)
      returns a spec-shaped response with `X-LGW-Request-Id`; the `requests` row has provider
      tokens, non-zero `cost_usd`, and a `price_id`.
- [ ] Cost math: `prices add` a second row with a later `effective_at`; a new request uses it, the
      old request row is unchanged.
- [ ] Missing auth, bad key, unknown model, `tools` present, and oversize body each return their
      documented status and envelope (tested with the mock transport; no live calls in pytest).
- [ ] Logs are JSON lines with request ids; no message content, no keys. CI green;
      `uv run ruff check .`, `uv run black --check .`, `uv run pytest` pass locally.

---

## Phase 2 - Streaming passthrough and provider translation

**Goal**: `stream: true` proxies SSE with correct usage accounting and disconnect handling, and
Anthropic and Gemini serve requests behind the same OpenAI-shaped surface.

### Tasks

- `services/streaming.py`: SSE relay, `include_usage` injection, usage capture from the stream,
  ttfb/latency, estimate fallback with `tokens_estimated`, client-disconnect commit with status
  `cut_off`, mid-stream upstream error event.
- Anthropic adapter: request translation (system hoist, required max_tokens), response and error
  translation, stream event translation to OpenAI chunks.
- Gemini adapter: same for generateContent and `streamGenerateContent?alt=sse`.
- `GET /v1/models` listing configured public models.

### Expected commits

1. `feat(streaming): add sse passthrough with usage capture`
2. `feat(streaming): commit cost on client disconnect`
3. `feat(providers): add anthropic adapter`
4. `feat(providers): add anthropic stream translation`
5. `feat(providers): add gemini adapter`
6. `feat(providers): add gemini stream translation`
7. `feat(models): add models listing endpoint`
8. `test(phase2): cover streaming accounting and translation`
9. `docs: update readme with provider configuration`

### Verification checklist

- [ ] A streamed OpenAI-route request via the SDK yields identical concatenated content to the
      provider's own answer; the request row records provider-reported usage
      (`tokens_estimated = false`), cost, ttfb, and latency.
- [ ] The client only receives a final usage chunk when it sent `stream_options.include_usage`.
- [ ] Kill the client mid-stream (curl + ctrl-c): upstream closes promptly, row status `cut_off`,
      cost committed for observed tokens.
- [ ] Mock upstream dies mid-stream after N chunks: client receives an SSE error event and
      `[DONE]`-less termination; row `failed` with cost for the N chunks.
- [ ] Anthropic and Gemini routes: non-streamed and streamed requests return spec-shaped OpenAI
      responses (roles, finish_reason mapping, usage); provider 400s surface as translated
      envelopes with the upstream message.
- [ ] `GET /v1/models` lists exactly the models with active routes. Lint/format/tests green; all
      provider tests use `httpx.MockTransport` fixtures with recorded wire shapes.

---

## Phase 3 - Routing, failover, and race-safe budgets

**Goal**: multi-route models fail over without double-billing, and per-key monthly budgets hold
under concurrency with reservation, commit, release, and sweep.

### Tasks

- `model_routes` table usage: `lgw routes set|list`, route resolution, startup validation
  (every routed model has a price row; provider keys present for used providers).
- `services/router.py` attempt loop: failover on timeout/connect/5xx/429, no failover on other
  4xx, no failover after first client byte, attempts rows, exhaustion mapping (502 or all-429).
- `services/budget.py`: lazy period creation, conditional-UPDATE reserve, single-winner
  commit/release, the sweeper task, 429 `budget_exceeded` envelope.
- `lgw usage` report (per key, per month, tables and `--json`) and `lgw usage --verify`
  reconciliation.

### Expected commits

1. `feat(routes): add model route table with cli management`
2. `feat(startup): validate routes, prices, and provider credentials`
3. `feat(router): add priority failover on timeout, 5xx, and rate limit`
4. `feat(budget): add atomic reservation against monthly periods`
5. `feat(budget): commit actual cost and release on failure`
6. `feat(budget): add stale reservation sweeper`
7. `feat(cli): add usage report with verify mode`
8. `test(phase3): cover failover billing and concurrent budget races`
9. `docs: update readme with routing and budget setup`

### Verification checklist

- [ ] Route 1 mocked to 500/timeout/429 in turn: request succeeds via route 2; audit shows two
      attempts with correct outcomes; exactly one attempt is `ok`; cost derives from route 2's
      usage and price only.
- [ ] Provider 400 on route 1: no failover, translated envelope returned, reservation released.
- [ ] All routes down: one 502 `upstream_error`; all routes 429: one 429 `upstream_rate_limited`.
- [ ] Concurrency test: budget with room for 3 of 10 parallel requests admits at most the
      affordable subset (asyncio.gather against the app); the rest get 429 `budget_exceeded`;
      `spent + reserved` never exceeds the cap during the run.
- [ ] Kill the process between reserve and commit (simulated in test): sweeper releases the
      reservation after TTL, request row becomes `failed` with `error_code: "swept"`, and
      `lgw usage --verify` reconciles.
- [ ] Budget change mid-month applies to the next request. Startup with a routed model missing a
      price row refuses to boot with a clear message. Lint/format/tests green.

---

## Phase 4 - PII redaction and semantic cache

**Goal**: prompts leave the gateway redacted and come back restored (including streams), and
opted-in keys get exact and semantic cache hits with zero PII at rest.

### Tasks

- `services/redaction.py`: email/phone/Luhn-card detectors, stable placeholder numbering,
  in-memory map, response restoration, streaming tail buffer.
- `services/cache.py`: exact layer (SQLite source of truth, optional Redis accelerator with
  silent degradation), embedding client, bounded cosine scan, threshold match,
  placeholder-mismatch miss rule, TTL and per-key cap eviction task, streamed hit replay.
- Wire into the chat flow per the lifecycle order in `docs/architecture.md` (redact before
  estimate/embed/upstream; cache between budget and attempt loop).

### Expected commits

1. `feat(redaction): add pii detectors with placeholder mapping`
2. `feat(redaction): restore placeholders in responses and streams`
3. `feat(cache): add exact match layer backed by sqlite`
4. `feat(cache): add redis accelerator with sqlite fallback`
5. `feat(cache): add embedding client and semantic lookup`
6. `feat(cache): replay cache hits for streaming requests`
7. `feat(cache): add ttl and per key cap eviction`
8. `test(phase4): cover redaction, restoration, and cache tiers`
9. `docs: update readme with privacy and cache sections`

### Verification checklist

- [ ] A prompt with an email, two phone formats, a Luhn-valid card, and a Luhn-invalid lookalike
      reaches the mock upstream with exactly three placeholder types and the lookalike untouched;
      the client response contains the originals; DB, logs, and the embedding request contain
      neither originals nor map.
- [ ] Streaming restoration: mock upstream splits a placeholder across two chunks; client
      receives the restored value; zero-redaction streams are verified unbuffered (chunk count
      in equals chunk count out).
- [ ] Identical request on an opted-in key: `X-LGW-Cache: hit`, zero cost, no upstream call, usage
      reported from the stored entry; non-opted-in key always misses; key B never hits key A's
      entry for the same prompt.
- [ ] Paraphrase above the threshold hits semantically; below it misses (deterministic fake
      embedding provider in tests); cached response referencing a placeholder absent from the
      new request's map is discarded as a miss.
- [ ] Stop Redis mid-run: warning logged once, hits keep working via SQLite; restart Redis:
      accelerator resumes. Eviction: entries past TTL and beyond the per-key cap are pruned.
- [ ] Lint/format/tests green; no live embedding or provider calls in pytest.

---

## Phase 5 - Hardening and launch readiness

**Goal**: the gateway survives hostile input and operational reality: body limits, graceful
shutdown, request summary logging, and the launch checklist verified.

### Tasks

- Enforce `LGW_MAX_BODY_KB` with 413 before JSON parsing; reject unsupported parameters with
  precise messages (`extra="forbid"` audit of the schema).
- Graceful shutdown: stop accepting, drain active streams up to a deadline, sweep on boot.
- One `request.completed|failed|cut_off` summary log line per request with status, provider,
  tokens, cost, latency, cache state.
- Finalize README (real install/run/test), verify `docs/testing.md` commands as written, walk
  `docs/launch-checklist.md`.

### Expected commits

1. `feat(limits): enforce request body size limit`
2. `feat(server): add graceful shutdown draining active streams`
3. `feat(audit): add per request summary log line`
4. `test(phase5): cover limits and shutdown behavior`
5. `docs: finalize readme and verify launch checklist`

### Verification checklist

- [ ] A body over `LGW_MAX_BODY_KB` gets 413 without being parsed; a malformed JSON body gets
      400 with the envelope, never a traceback.
- [ ] SIGTERM during an active stream: the stream completes (or hits the drain deadline), the
      audit row is finalized, no reservation is left `held` after the boot-time sweep.
- [ ] Every terminal request emits exactly one summary log line; a day of manual traffic shows
      only documented event keys and zero ERROR lines.
- [ ] README commands executed verbatim on a clean checkout succeed; the full suite and lint are
      green; every unchecked launch-checklist item has an owner decision recorded.

---

## Backlog

- Anthropic prompt-caching passthrough (cost model differs per cached token; needs price schema
  thought). Deferred: not needed for correctness.
- `lgw prune` retention command for old `requests`/`attempts` rows. Deferred: audit growth is
  slow at self-hosted volumes; revisit when a real deployment reports size pressure.
- Tool/function-call translation across providers. Deferred: large surface, out of v1 scope by
  PRD non-goal.
- Per-key rate limiting (requests/minute). Deferred: budgets bound spend, which is the v1 harm
  model; add if a deployment needs concurrency protection.
