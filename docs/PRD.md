# Product Requirements - llm-gateway

## What we are building

A self-hosted LLM API gateway. Applications call one OpenAI-compatible endpoint
(`POST /v1/chat/completions`) with a gateway-issued virtual key. The gateway authenticates the
key, redacts PII (emails, phone numbers, credit cards) from the prompt before it leaves the box,
enforces the key's monthly USD budget with an atomic reservation, serves a semantic cache hit when
the key has opted in, and otherwise routes the request to the first healthy provider in a
configured priority order (OpenAI, Anthropic, Gemini), translating between the OpenAI wire format
and each provider's native API. Streaming responses pass through as SSE while the gateway still
captures token counts and cost. Every request writes an audit row (key, provider, model, tokens,
cost, latency, cache state) that never contains prompt or response bodies. Cost is computed from a
versioned price table so historical numbers stay correct after price changes. An admin CLI manages
keys, routes, and prices, and reports usage as tables or JSON.

## Target user

A developer or small team running several LLM-backed applications who wants one control point in
front of the provider APIs: a single key surface, per-app spending caps that cannot be raced past,
automatic failover when a provider degrades, a privacy layer that keeps customer PII out of
third-party APIs, and an audit trail of what was spent where. Single instance, self-hosted, bring
your own provider API keys. Secondary audience: a reviewer of this repository evaluating how
gateway concerns (streaming accounting, race-safe budgets, wire-format translation) should be
engineered.

## Core features (prioritized)

1. **OpenAI-compatible chat endpoint** (highest priority). `POST /v1/chat/completions` accepts the
   OpenAI request shape (text chat only) and returns the OpenAI response shape, streaming or not,
   regardless of which upstream provider served it. Existing OpenAI SDKs work by changing only
   `base_url` and `api_key`.
2. **Virtual-key authentication.** Keys are created by the admin CLI, shown once, and stored only
   as a SHA-256 hash. Revocation is immediate. Every other feature (budget, cache opt-in, audit)
   hangs off the key.
3. **Provider routing with failover.** Each public model name maps to an ordered list of
   (provider, provider model) routes. On timeout, connection failure, 5xx, or 429 the gateway
   fails over to the next route. Failover never happens after the first response byte has been
   sent to the client, and a request is never billed for more than one successful attempt.
4. **SSE streaming passthrough.** `stream: true` proxies upstream chunks to the client as they
   arrive, translated to OpenAI chunk format, while the gateway captures provider-reported usage
   (or a flagged estimate) for cost accounting, including on client disconnect.
5. **Per-key monthly USD budgets with hard cutoff.** Before any upstream call the gateway
   atomically reserves an estimated cost against the key's calendar-month budget in a single
   conditional update; over-budget requests are rejected. Actual cost is committed on success and the
   reservation is released on failure, so concurrent requests cannot overshoot the cap.
6. **Cost tracking from a versioned price table.** Prices are append-only rows with an
   effective-from timestamp. Each request stores the computed cost and the price row used, so a
   price change never rewrites history.
7. **PII redaction.** Emails, phone numbers, and Luhn-valid card numbers in prompts are replaced
   with placeholders before the request leaves the gateway; the mapping lives only in request
   memory and placeholders are restored in the response, including mid-stream.
8. **Semantic response cache (opt-in per key).** An exact-match layer plus an embedding-similarity
   layer over recent redacted prompts, scoped per key and model. Redis accelerates the exact layer
   when configured; the default install uses only SQLite.
9. **Audit log and admin CLI.** Every request writes one metadata row plus one row per upstream
   attempt. The `lgw` CLI creates and revokes keys, sets budgets, manages routes and prices, and
   prints usage reports (tables or `--json`) including a spend reconciliation check.

## Non-goals

- No web dashboard. The CLI and JSON reports are the entire admin surface.
- No fine-tuning, image models, audio, or embeddings-as-a-service passthrough. Text chat only:
  requests carrying `tools`, image content parts, or `n > 1` are rejected with a clear error.
- No multi-node clustering or shared-nothing horizontal scale. One process, one SQLite file;
  Redis is an optional cache accelerator, never required state.
- No provider account management: the operator supplies provider API keys via environment
  variables; the gateway never creates or rotates them.
- No per-request user attribution beyond the virtual key. Keys are the tenancy unit.
- No prompt or response body storage by default; `LGW_LOG_BODIES` exists for debugging and logs
  redacted bodies only, to the structured log, never to the database.

## Success criteria per core feature

- **OpenAI-compatible endpoint** - The official OpenAI Python SDK pointed at the gateway completes
  a chat, a streamed chat, and receives spec-shaped errors, against all three providers, with no
  client code changes beyond `base_url` and `api_key`.
- **Virtual keys** - A created key authenticates; a revoked key gets 401 within one request; the
  raw key appears nowhere in the database or logs (only hash and last four characters).
- **Routing and failover** - With route 1 forced to 500/timeout/429, requests succeed via route 2
  and the audit shows both attempts with outcomes; a 4xx from the provider does not fail over.
  With all routes failing, the client gets one 502 (or 429 when every attempt was rate-limited).
- **Streaming** - Streamed content is byte-equivalent to the provider's content; usage and cost
  are recorded within one row of provider-reported numbers; a client disconnect mid-stream still
  produces an audit row with cost committed and status `cut_off`.
- **Budgets** - N concurrent requests against a budget with room for fewer than N succeed for at
  most the affordable subset and 429 the rest; the month's summed request costs never exceed the
  cap plus one in-flight estimate; `lgw usage --verify` reports spent equal to summed costs.
- **Cost tracking** - Adding a new price row changes the cost of new requests only; re-running a
  usage report for a past month is byte-identical before and after the price change.
- **PII redaction** - A prompt containing an email, a phone number, and a card number reaches the
  mock upstream with placeholders only; the client response contains the original values; the
  database and logs contain neither. Streaming restoration survives a placeholder split across
  chunk boundaries.
- **Semantic cache** - An identical prompt on an opted-in key is served from cache with
  `X-LGW-Cache: hit`, zero cost, and no upstream call; a paraphrase above the similarity threshold
  also hits; a different key never sees another key's entries; with Redis down the cache still
  works via SQLite.
- **Audit and CLI** - Every request (success, failure, cache hit, cut-off) has exactly one request
  row; `lgw keys|routes|prices|usage` behave per `docs/api-contracts.md` with exit codes 0/1/2 and
  clean `--json` output.
