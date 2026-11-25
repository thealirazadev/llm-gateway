# Design decisions

## 1. Plain httpx, no provider SDKs

Upstream calls are plain httpx requests against each provider's documented wire format. The
`openai`, `anthropic`, and `google-genai` packages are not dependencies. Translation between the
OpenAI shape and each native API is explicit code in `app/providers/`, which keeps it testable
with `httpx.MockTransport` fixtures of recorded wire shapes and keeps every provider-specific
branch inside its adapter. The rest of the codebase speaks canonical OpenAI shapes only.

## 2. Adapters emit stream events, one serializer owns the chunk format

Each adapter translates its provider's stream into a small `StreamEvent` (role, content,
finish reason, token counts), and `services/streaming.py` renders the OpenAI chunk frame. The
chunk envelope varies only by request id, model, and created timestamp, none of which depend on
the provider, so building it in three adapters would have triplicated the framing rules fixed in
`docs/api-contracts.md`. `schemas.chunk_json` is the single place that knows the wire shape.

## 3. The failover boundary is the first byte sent to the client

Before any byte reaches the client, every failover trigger (timeout, connection failure, 5xx,
429) is safe: the failed attempt produced no billable success and no visible output. After the
first byte, switching providers would emit duplicated or inconsistent content and could bill two
providers for one request. So a mid-stream failure ends the stream with an SSE error event and
commits cost for the tokens actually consumed. The streaming attempt row is written once, at
stream end, with the outcome the stream reached. Cost always derives from exactly one attempt,
which is what makes "never billed twice" hold.

## 4. Provider credential errors fail over and are never relayed

A provider 401 or 403 fails over to the next route instead of being returned to the caller. A
smoke run against the real OpenAI API with a bad key showed why: the caller got a 400 that
blamed them and quoted a masked fragment of the operator's provider key. A rejected credential is
the operator's problem, nothing was billed, so failover is safe. The event is logged as
`upstream.credentials_rejected`, and if every route is exhausted the caller gets the generic 502.

## 5. Budgets use reservations, not check-then-write

Check-then-write has an unavoidable race window under concurrency. Instead, an estimate is held
against the key's monthly period by one conditional UPDATE, the actual cost is committed on
success, and the hold is released on failure. SQLite has no `SELECT ... FOR UPDATE`, but its
single-writer model makes the conditional UPDATE atomic, which is a real benefit of choosing
SQLite here. Stale holds are swept after a TTL by a task scheduled in the app lifespan. Commit,
release, and sweep all guard on `state = 'held'`, and the sweep's update of the request row
guards on `in_flight`, so exactly one transition wins. Two bugs were found while wiring this into
the request path: SQLAlchemy's generic `insert` has no `on_conflict_do_nothing`, so the period
upsert needs the SQLite dialect insert, and the estimate multiplied a float by a `Decimal`. A
test fires 10 concurrent requests at a budget that affords fewer and asserts the cap holds.

A third bug was found in review: a request that outlived `LGW_RESERVATION_TTL_SECONDS` was swept
while it ran, and its later success then found no `held` reservation and no `in_flight` row, so the
cost it incurred upstream was never added to `spent_usd` or to the request row. The late commit now
moves the swept reservation from `released` to `committed` once, adds the actual cost to
`spent_usd` without touching `reserved_usd` (the sweep already refunded the hold), and writes the
outcome over the `failed(swept)` row, which keeps `spent_usd == SUM(requests.cost_usd)`.

## 6. Over budget is 429 `insufficient_quota`

A rejected reservation returns 429 with `error.type: "insufficient_quota"` rather than 402.
OpenAI signals an exhausted account the same way, so existing SDKs classify it as a
non-retryable quota error instead of retry-looping against a hard cap, which a 402 or a plain
rate-limit 429 would invite.

## 7. Provider-reported token counts, not a local tokenizer

Token counts always come from the provider, including in streams: OpenAI's `include_usage`,
Anthropic's `message_start` and `message_delta`, Gemini's `usageMetadata`. tiktoken only matches
OpenAI models, so local counting would be wrong for two of three providers and add a dependency.
A `ceil(chars/4)` estimate is used only when a stream dies before usage arrives, and the row is
flagged `tokens_estimated` so reports stay honest. The gateway always sends
`stream_options.include_usage` to OpenAI, but forwards the usage chunk to the client only when
the client asked for it: accounting must not depend on a client flag, and the client contract
must not change because of an internal need.

## 8. Streams are finalized in a background task

When a client disconnects, Starlette cancels the sending task and leaves the relay generator
suspended at a `yield`; it is only closed later by garbage collection. Finalizing in the
generator's `finally` would commit cost late or not at all. The response's `BackgroundTask` is
the one point every ending passes through, and it runs after cancellation is absorbed, so it can
still close the upstream. A live test showed the row marked `cut_off` two seconds in while the
mock upstream still had frames queued. A related bug: Starlette runs the background task only if
the body iterator ends without raising, and the relay caught only provider and httpx errors. A
malformed usage chunk (token counts of `null`) raised `TypeError`, so the row stayed `in_flight`,
no attempt was written, and generated tokens were never billed. The relay now catches
`Exception` last and records `bad_response`; disconnects raise `BaseException` subclasses and
still reach the `cut_off` path. Because the request-id context variable is reset before the body
streams, streaming log lines carry the request id explicitly.

## 9. Provider stream quirks

Anthropic's `message_start` carries the final input token count but only the output tokens
emitted so far, which is 1. Reporting both meant a stream cut off before `message_delta` was
billed for one output token and flagged as exact. Only the input count is taken from
`message_start` now, so the estimate applies and is flagged. Gemini's `usageMetadata` can arrive
before any output, and an absent `candidatesTokenCount` was read as zero, billing the output as
free; counts are now used only when the chunk carries them. Unknown stop reasons map to `null`
on a non-streaming response but to `"stop"` inside a stream, because a stream without a
terminating chunk hangs SDK parsers, which is worse than an imprecise reason.

## 10. Synchronous SQLAlchemy with short-lived sessions

Database access is synchronous SQLAlchemy called from async handlers. Sessions are scoped per
operation and never held across an upstream call or a stream. SQLite is a local file, so the
blocking window is microseconds, while all network I/O stays async. This avoids adding an async
driver as a dependency.

## 11. Redaction runs first, and the cache never holds PII

Emails, phone numbers, and Luhn-valid card numbers are replaced with placeholders before the
budget estimate, the cache lookup, or the upstream call. The placeholder map lives only in
request memory. Phone detection excludes date shapes, because a probe showed dates such as
2026-11-09 matching the phone pattern. The cache embeds and stores only redacted content, keeps
placeholders unrestored, and is scoped per key and model. A hit is discarded when the cached
answer references a placeholder absent from the current request's map, so one request's PII can
never be restored into another's response. The cost is a slightly lower hit rate on PII-heavy
prompts. In streams, a placeholder can be split across chunks, so a small tail buffer holds back
a possible partial placeholder, bounded by `MAX_PLACEHOLDER_CHARS`. When nothing was redacted
there is no buffering at all. The semantic layer is a pure-Python cosine scan over the newest
`LGW_CACHE_SCAN_LIMIT` entries rather than a vector index or numpy.

## 12. Append-only, versioned prices

Prices are append-only rows with an `effective_at` timestamp, and each request stores its cost
and the price row used, so a price change never rewrites history. If a successful call has no
price row, it is finalized `ok` with `cost_usd = 0` and a `prices.missing` error log rather than
failing a request the operator already paid for. Startup validation refuses to boot when a routed
model has no price row, which makes that branch unreachable in practice.

## 13. Secrets, input, and logs

Virtual keys are stored only as a SHA-256 hash plus the last four characters; the CLI prints the
raw key once. Revocation is checked on every request. The request schema uses `extra="forbid"`
so unsupported OpenAI parameters fail with `unsupported_parameter` instead of being silently
dropped, and the body size limit is enforced before parsing. Outbound calls go only to the three
provider base URLs pinned in `app/config.py`, never to URLs taken from request content. Logs are
JSON lines with a request id and never contain prompt or response bodies, PII values,
placeholder maps, raw virtual keys, or provider keys.
