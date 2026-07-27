# Project Memory - llm-gateway

Running log of what is done, in progress, and decided. Update after every meaningful chunk of
work; log every non-obvious decision with its reason. Keep entries short and dated.

## Completed

- 2026-07-27 - Planning documentation created (README, PRD, architecture, rules, phases, design,
  testing, api-contracts, launch-checklist, memory). No code yet; docs await owner review before
  Phase 1 starts.

## Project status

- Planning stage. Implementation follows `docs/phases.md` phase by phase, one coding agent, one
  commit per feature, starting only after the owner approves these documents.

## Decisions log

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
