# API Contracts - llm-gateway

Two surfaces exist: the OpenAI-compatible **HTTP API** (three routes) and the **`lgw` admin CLI**.
Both are fixed here before any code is written. Timestamps are ISO-8601 UTC; money is USD.

Base URL: wherever the operator runs uvicorn, e.g. `http://127.0.0.1:8000`.

## Authentication

Every HTTP route except `GET /health` requires:

```
Authorization: Bearer lgw_<40 url-safe characters>
```

Keys are issued by `lgw keys create`, stored server-side only as SHA-256 hashes, and checked for
revocation on every request. Missing, malformed, unknown, or revoked keys get 401.

## Error envelope (all JSON errors, all routes)

OpenAI-compatible so existing SDKs raise their native exception types:

```json
{
  "error": {
    "message": "Monthly budget exceeded for this key.",
    "type": "insufficient_quota",
    "code": "budget_exceeded"
  }
}
```

### Stable error codes

| HTTP | `error.code` | `error.type` | When |
|---|---|---|---|
| 401 | `invalid_api_key` | `invalid_request_error` | Missing/unknown/revoked virtual key. |
| 404 | `model_not_found` | `invalid_request_error` | No active route for the requested model. |
| 400 | `unsupported_parameter` | `invalid_request_error` | `tools`, image content, `n > 1`, or unknown fields. |
| 400 | `invalid_request` | `invalid_request_error` | Malformed JSON or schema violation (message names the field). |
| 400 | `upstream_rejected` | `invalid_request_error` | The winning provider returned a non-429 4xx; upstream message included. |
| 413 | `payload_too_large` | `invalid_request_error` | Body exceeds `LGW_MAX_BODY_KB`. |
| 429 | `budget_exceeded` | `insufficient_quota` | Monthly budget reservation rejected. SDKs treat `insufficient_quota` as non-retryable. |
| 429 | `upstream_rate_limited` | `rate_limit_error` | Every route returned 429. |
| 502 | `upstream_error` | `api_error` | All routes failed (5xx/timeout/connect/bad response). |
| 500 | `server_error` | `api_error` | Unexpected gateway bug (details logged, never returned). |

Nothing about the prompt appears in any error message. Mid-stream failures use the same envelope
inside a final SSE `data:` event (see Streaming).

---

## POST /v1/chat/completions

OpenAI chat request shape, text content only. Accepted fields: `model`, `messages` (roles
`system` | `user` | `assistant`, string `content`), `stream`, `stream_options.include_usage`,
`temperature`, `top_p`, `max_tokens`, `stop`. Any other field: 400 `unsupported_parameter`.

### Request

```json
{
  "model": "gpt-4.1-mini",
  "messages": [
    {"role": "system", "content": "You are a support assistant."},
    {"role": "user", "content": "Summarize the ticket from jane@example.com."}
  ],
  "max_tokens": 300
}
```

`model` is the public name configured in `model_routes`; the gateway picks the provider. The
email above is replaced with `<pii_email_1>` before any upstream or embedding call and restored
in the response.

### Response 200 (non-streaming)

```json
{
  "id": "chatcmpl-01J2ZK8Q4V9WXY5T3M1N7P6R2S",
  "object": "chat.completion",
  "created": 1753600000,
  "model": "gpt-4.1-mini",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "The ticket from jane@example.com asks..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 184, "completion_tokens": 96, "total_tokens": 280}
}
```

`id` is `chatcmpl-` + the gateway request ULID, identical to `X-LGW-Request-Id`. `usage` is
provider-reported; on a cache hit it is the stored entry's usage. `finish_reason` is the
canonical OpenAI value regardless of provider (`stop`, `length`, `content_filter`).

### Response headers (every completion response, success or error)

| Header | Value |
|---|---|
| `X-LGW-Request-Id` | ULID; audit row primary key; quote it in support requests. |
| `X-LGW-Provider` | `openai` \| `anthropic` \| `gemini`; absent on cache hits and pre-route errors. |
| `X-LGW-Cache` | `hit` \| `miss` \| `off` (key not opted in). |

### Streaming (`"stream": true`)

`Content-Type: text/event-stream`. OpenAI chunk framing exactly; SDK stream parsers work
unmodified:

```
data: {"id":"chatcmpl-01J2...","object":"chat.completion.chunk","created":1753600000,"model":"gpt-4.1-mini","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-01J2...","object":"chat.completion.chunk","created":1753600000,"model":"gpt-4.1-mini","choices":[{"index":0,"delta":{"content":"The ticket"},"finish_reason":null}]}

data: {"id":"chatcmpl-01J2...","object":"chat.completion.chunk","created":1753600000,"model":"gpt-4.1-mini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

- A final usage chunk (empty `choices`, populated `usage`) is sent only when the client asked via
  `stream_options: {"include_usage": true}`; the gateway always captures usage internally either
  way.
- Upstream failure after streaming began: one final event
  `data: {"error": {"message": "...", "type": "api_error", "code": "upstream_error"}}` and the
  connection closes without `[DONE]`. Failures before the first content chunk are ordinary JSON
  errors with the appropriate status (failover already happened invisibly).
- Cache hits under streaming replay as: role chunk, one content chunk, finish chunk, `[DONE]`.

---

## GET /v1/models

Lists public models that have at least one active route, OpenAI list shape:

```json
{
  "object": "list",
  "data": [
    {"id": "gpt-4.1-mini", "object": "model", "created": 1753600000, "owned_by": "llm-gateway"},
    {"id": "claude-sonnet", "object": "model", "created": 1753600000, "owned_by": "llm-gateway"}
  ]
}
```

## GET /health

Public, no auth. `200 {"status": "ok"}` when the process is up and the database answers a trivial
query; `503 {"status": "degraded"}` otherwise. Redis state never affects health (it is optional
by design).

---

## Admin CLI (`lgw`)

Direct database access via `DATABASE_URL` (or `--db`); no HTTP, no running server needed. Exit
codes: 0 success, 1 operation failure, 2 usage/environment error. `--json` where shown emits a
single JSON object to stdout. Full UX rules in `docs/design.md`.

### lgw keys create

```
$ lgw keys create --name billing-app --budget 50 --cache
key: lgw_h1J9x2... (shown once, store it now)
created key 'billing-app'  budget=$50.00/month  cache=on
```

The raw key line goes to stderr so `$(...)` captures only if intended; the summary goes to
stdout. Duplicate name: `error: key 'billing-app' already exists`, exit 1.

### lgw keys list

```
$ lgw keys list
NAME         LAST4  BUDGET_USD  CACHE  ACTIVE  CREATED
billing-app  x9f2        50.00  on     yes     2026-07-27T10:12:03Z
support-bot  k2m8            -  off    yes     2026-07-27T10:14:44Z
```

### lgw keys revoke / set-budget

```
$ lgw keys revoke support-bot --yes
revoked key 'support-bot'

$ lgw keys set-budget billing-app 75
budget for 'billing-app' set to $75.00/month (applies immediately)
```

### lgw routes set / list

```
$ lgw routes set gpt-4.1-mini openai:gpt-4.1-mini anthropic:claude-3-5-haiku-latest
routes for 'gpt-4.1-mini':
  1. openai:gpt-4.1-mini
  2. anthropic:claude-3-5-haiku-latest
```

Atomic replacement in the given order. Refused with exit 1 and no change when a provider's API
key is not configured or a `provider:model` has no price row; the message names every missing
piece. `lgw routes list` prints the same numbered format for all models.

### lgw prices add / list

```
$ lgw prices add openai gpt-4.1-mini --input 0.40 --output 1.60
added price #7: openai gpt-4.1-mini in=$0.40/Mtok out=$1.60/Mtok effective 2026-07-27T10:20:00Z

$ lgw prices list --provider openai
ID  PROVIDER  MODEL         IN_PER_MTOK  OUT_PER_MTOK  EFFECTIVE_AT
7   openai    gpt-4.1-mini         0.40          1.60  2026-07-27T10:20:00Z
3   openai    gpt-4.1-mini         0.15          0.60  2026-07-01T00:00:00Z
```

Append-only: no edit, no delete. A new row with a later `effective_at` changes future requests
only.

### lgw usage

```
$ lgw usage --month 2026-07
KEY          MONTH    REQUESTS  CACHE_HITS  PROMPT_TOK  COMPL_TOK  COST_USD
billing-app  2026-07      1842         311     2104332     398211   12.4183
TOTAL                     1842         311     2104332     398211   12.4183
```

```
$ lgw usage --month 2026-07 --json
{
  "month": "2026-07",
  "keys": [
    {
      "name": "billing-app",
      "requests": 1842,
      "cache_hits": 311,
      "prompt_tokens": 2104332,
      "completion_tokens": 398211,
      "cost_usd": "12.4183"
    }
  ],
  "total_cost_usd": "12.4183"
}
```

`--key NAME` filters; default month is the current UTC month; costs are strings in JSON to avoid
float drift.

### lgw usage --verify

```
$ lgw usage --verify --month 2026-07
billing-app  2026-07  spent=12.4183  sum(requests)=12.4183  reserved=0.0000  ok
```

Any mismatch prints a discrepancy line and exits 1; this is the reconciliation invariant from
`docs/architecture.md` made operational.

---

## Access summary

| Surface | Auth |
|---|---|
| `POST /v1/chat/completions`, `GET /v1/models` | Virtual key (Bearer). |
| `GET /health` | Public. |
| `lgw` CLI | Filesystem access to the database; there is deliberately no remote admin API. |
