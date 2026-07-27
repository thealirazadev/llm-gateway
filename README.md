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

## Planned features

All of the following is planned, not yet implemented; see the status line below.

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

Planning stage: documentation only, no implementation yet. Implementation follows
[docs/phases.md](docs/phases.md) one phase at a time, starting after owner review of these
documents.
