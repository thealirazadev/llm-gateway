# llm-gateway

A self-hosted LLM API gateway: one OpenAI-compatible endpoint in front of OpenAI, Anthropic, and
Gemini, with virtual keys, hard monthly budgets, automatic failover, and a complete cost audit
trail. PII redaction and a semantic response cache are planned. Point any OpenAI SDK at it by changing
`base_url` and `api_key`; the gateway handles provider routing, wire-format translation, and the
accounting.

## The problem it solves

Teams running several LLM-backed apps end up with provider keys scattered across services, no
per-app spending limit that holds under concurrent traffic, no failover when a provider has an
incident, customer PII flowing to third-party APIs unredacted, and no single answer to "what did
we spend, where, on which app, last month". llm-gateway is one control point that fixes all five,
self-hosted, with zero required infrastructure beyond Python and SQLite.

## Features

The full target feature set; the status section below says what is implemented today.

- OpenAI-compatible `POST /v1/chat/completions` (streaming and non-streaming) in front of
  OpenAI, Anthropic, and Gemini, with per-provider wire-format translation.
- Virtual keys with per-key monthly USD budgets enforced by atomic reservation: concurrent
  requests cannot race past the cap.
- Priority-ordered provider routing with automatic failover on timeout, connection failure, 5xx,
  and rate limit, designed so failover can never double-bill.
- SSE streaming passthrough that still captures exact token counts and cost, including on client
  disconnect.
- Cost tracking per request from an append-only, versioned price table: historical reports never
  change when prices do.
- PII redaction (emails, phone numbers, Luhn-valid card numbers) applied before prompts leave the
  gateway, reversed on the response via an in-memory placeholder map, streaming included.
- Opt-in response cache (exact match plus embedding similarity), scoped per key and model, stored
  in SQLite with redacted content only. A Redis accelerator is planned.
- Structured audit log of every request (key, provider, model, tokens, cost, latency, cache
  state) with no prompt bodies stored.
- `lgw` admin CLI: key, route, and price management, plus monthly usage reports (table or JSON)
  and `lgw usage --verify`, which reconciles each key's committed spend with its request costs.

## Stack

- Python 3.12, FastAPI, Pydantic
- SQLite via SQLAlchemy 2.x (Alembic migrations); optional Redis as a cache accelerator only
- httpx for upstream calls (no provider SDKs)
- pytest + pytest-asyncio; uv, ruff, black

## Run it

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # install pinned dependencies
cp .env.example .env                      # then set the provider keys you route to
uv run alembic upgrade head               # create the schema
uv run python -m app.cli keys create --name dev   # prints the key once
uv run python -m app.cli prices add openai gpt-4.1-mini --input 0.40 --output 1.60
uv run uvicorn app.main:app               # serves on http://127.0.0.1:8000
```

Or with Docker (SQLite lives on the `/data` volume; the container migrates on start):

```bash
docker build -t llm-gateway .
docker run -d --name lgw -p 8000:8000 --env-file .env -v lgw-data:/data llm-gateway
docker exec lgw lgw keys create --name dev
```

Then point any OpenAI client at the gateway:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer lgw_..." \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Hello"}]}'
```

Every response carries `X-LGW-Request-Id`, `X-LGW-Provider`, and `X-LGW-Cache`, and every request
writes an audit row with tokens, cost, and the price row used.

## Providers

Three providers sit behind the same OpenAI-shaped surface. Clients always send the OpenAI chat
format; the gateway translates the request, the response, the stream, and the error.

| Provider | Credential | Upstream API | Notes on translation |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `/v1/chat/completions` | Passthrough; usage is requested with `stream_options.include_usage`. |
| `anthropic` | `ANTHROPIC_API_KEY` | Messages API | System messages hoisted to `system`; `max_tokens` is required upstream and falls back to `LGW_DEFAULT_MAX_OUTPUT_TOKENS`. |
| `gemini` | `GEMINI_API_KEY` | `generateContent` | System messages hoisted to `systemInstruction`; the `assistant` role becomes `model`; sampling moves into `generationConfig`. |

Finish reasons are normalised to `stop`, `length`, and `content_filter` regardless of provider,
and token counts always come from what the provider reported, streams included.

A public model name maps to one or more provider models through `model_routes`, tried in the
order given. A timeout, connection failure, 5xx, or 429 moves on to the next route; any other 4xx
is returned to the client without failover. Only the route that answered is billed:

```bash
uv run lgw routes set claude-sonnet anthropic:claude-3-5-haiku-latest openai:gpt-4.1-mini
uv run lgw keys set-budget my-app 50
```

A key with a monthly budget holds the worst-case cost of each request before calling upstream,
then commits the actual cost or releases the hold. A request that does not fit gets 429
`insufficient_quota`. Holds left by a crash are released by a sweeper after
`LGW_RESERVATION_TTL_SECONDS`.

Add a price row for every routed `provider`/`provider_model` pair, otherwise the request is
served but costed at zero with a `prices.missing` error in the log.

`GET /v1/models` lists every public model that has at least one active route.

## Streaming

Send `"stream": true` and the gateway proxies the upstream SSE stream as OpenAI chunk frames,
one frame at a time, never buffering the whole answer:

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer lgw_..." \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4.1-mini", "stream": true,
       "messages": [{"role": "user", "content": "Hello"}]}'
```

- Usage is captured from the stream for every request, so cost is exact; the final usage chunk
  is forwarded to the client only when it sent `stream_options: {"include_usage": true}`.
- A stream that ends without provider usage falls back to a character estimate and the audit
  row is flagged `tokens_estimated`.
- An upstream failure before the first byte is an ordinary JSON error. After the first byte the
  stream ends with one SSE error event and no `[DONE]`, and the tokens already generated are
  still costed.
- A client that disconnects mid-stream closes the upstream call immediately; the audit row is
  finalised `cut_off` with the cost observed so far, since the provider billed for it.

## Privacy: PII redaction

Every prompt is redacted before budget estimation, caching, or any upstream call. Emails, phone
numbers (E.164 and common formats, at least 7 digits, dates excluded), and card numbers that pass
the Luhn check become `<pii_email_1>`, `<pii_phone_1>`, `<pii_card_1>`; the same value always
gets the same placeholder. The response, streamed or not, gets the originals back, and a
placeholder split across two stream chunks is still restored. The map lives in request memory
only: the database stores a count (`requests.redactions`), never the values. Placeholders the
model invents are left as they are, never filled with someone else's data.

## Response cache

Create a key with `--cache` to opt in. An identical request (same redacted messages, model, and
sampling parameters) is answered from SQLite with `X-LGW-Cache: hit`, zero cost, and the stored
usage; streamed requests get the hit replayed as a stream. Set `LGW_EMBEDDING_PROVIDER`
(`openai` or `gemini`) and `LGW_EMBEDDING_MODEL` to also match paraphrases above
`LGW_CACHE_SIMILARITY_THRESHOLD` (default 0.93). Entries never cross keys, expire after
`LGW_CACHE_TTL_HOURS`, and are capped per key; a background task prunes them. If the embedding
provider fails, the request simply misses and goes upstream.

## Test it

```bash
uv run pytest                             # full suite, hermetic, no network
uv run ruff check .                       # lint
uv run black --check .                    # formatting
```

The suite never calls a provider: upstreams are `httpx.MockTransport` fixtures and each test runs
against its own migrated SQLite file.

## Documentation

| Document | Contents |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Problem, target user, features, non-goals, success criteria. |
| [docs/architecture.md](docs/architecture.md) | Stack rationale, data model, flows, failure modes, invariants, layout. |
| [docs/rules.md](docs/rules.md) | Project-specific engineering rules. |
| [docs/phases.md](docs/phases.md) | Implementation phases with exact commit lists and verification checklists. |
| [docs/design.md](docs/design.md) | CLI UX: commands, flags, output, exit codes. |
| [docs/testing.md](docs/testing.md) | Test strategy, coverage split, commands, CI plan. |
| [docs/api-contracts.md](docs/api-contracts.md) | Every endpoint and CLI command with examples and the error envelope. |
| [docs/launch-checklist.md](docs/launch-checklist.md) | Pre-launch verification. |
| [docs/memory.md](docs/memory.md) | Working log and decisions. |

## Status

Phases 1 and 2 are implemented: config, structured logging, the error envelope, the full schema
and migration, virtual-key authentication, key and price management in the CLI, the versioned
cost calculator, non-streaming and streaming `POST /v1/chat/completions` with exact accounting,
the OpenAI, Anthropic, and Gemini adapters, and `GET /v1/models`.

Phase 3 is implemented: route management, startup validation, priority failover, race-safe
monthly budgets with a scheduled sweeper, and `lgw usage` with `--verify` reconciliation, covered
by failover, concurrency, and CLI tests. Phase 4 is implemented except the optional Redis accelerator: PII redaction with streaming
restoration, and the exact and semantic cache with eviction. Implementation follows
[docs/phases.md](docs/phases.md) one phase at a time.
