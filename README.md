# llm-gateway

A self-hosted LLM API gateway: one OpenAI-compatible endpoint in front of OpenAI, Anthropic, and
Gemini, with virtual keys, hard monthly budgets, automatic failover, PII redaction, a semantic
response cache, and a complete cost audit trail. Point any OpenAI SDK at it by changing
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
- PII redaction (emails, phone numbers, credit cards) applied before prompts leave the gateway,
  reversed on the response via an in-memory placeholder map, streaming included.
- Opt-in semantic response cache (exact match plus embedding similarity), scoped per key, with
  optional Redis acceleration and a pure-SQLite default.
- Structured audit log of every request (key, provider, model, tokens, cost, latency, cache
  state) with no prompt bodies stored.
- `lgw` admin CLI: key, route, and price management plus usage reports as tables or JSON with a
  spend reconciliation check.

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

A public model name maps to a provider model through `model_routes`. The `lgw routes` commands
arrive with the routing phase, so for now insert the row directly:

```bash
sqlite3 data/gateway.db "INSERT INTO model_routes (model, position, provider, provider_model, active)
  VALUES ('claude-sonnet', 1, 'anthropic', 'claude-3-5-haiku-latest', 1);"
```

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
the OpenAI, Anthropic, and Gemini adapters, and `GET /v1/models`. Failover, budgets, PII
redaction, and the semantic cache land in later phases. Implementation follows
[docs/phases.md](docs/phases.md) one phase at a time.
