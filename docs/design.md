# Design - llm-gateway

There is no web UI. The product's two surfaces are the OpenAI-compatible HTTP API (whose UX is
"your existing SDK works", contract in `docs/api-contracts.md`) and the `lgw` admin CLI. This file
fixes the CLI's UX: command tree, flags, output, and exit codes. The CLI talks directly to the
database via the same SQLAlchemy models as the server; it needs `.env` (or `--db`), never a
running gateway.

## Principles

- Boring and scriptable: plain text tables for humans, `--json` for machines, nothing
  interactive except explicit confirmation prompts for destructive actions.
- Secrets are shown exactly once, at creation, and never again by any command.
- Every command is idempotent to re-run or fails loudly; no partial writes.
- Output goes to stdout, diagnostics to stderr, so pipes stay clean.

## Command tree

```
lgw keys create --name NAME [--budget USD] [--cache]
lgw keys list
lgw keys revoke NAME [--yes]
lgw keys set-budget NAME USD
lgw routes set MODEL PROVIDER:MODEL [PROVIDER:MODEL ...]
lgw routes list
lgw prices add PROVIDER MODEL --input USD_PER_MTOK --output USD_PER_MTOK [--effective-at ISO8601]
lgw prices list [--provider P] [--model M]
lgw usage [--month YYYY-MM] [--key NAME] [--json]
lgw usage --verify [--month YYYY-MM]
```

Global flags: `--db URL` (overrides `DATABASE_URL`), `--json` where listed. `NO_COLOR` and
non-TTY stdout both disable the minimal color use (status column only).

## Command behavior

- **keys create** prints a two-line result: the raw key (`lgw_` + 40 url-safe chars) with a
  "shown once, store it now" warning on stderr, and the created row summary. Duplicate name:
  error, exit 1.
- **keys revoke** asks "Revoke key NAME? [y/N]" unless `--yes`; revocation is immediate and
  irreversible (create a new key instead of un-revoking).
- **routes set** replaces MODEL's route list atomically in the given priority order. It refuses
  (exit 1, no change) when a named provider has no configured API key or when any
  `PROVIDER:MODEL` lacks a price row, naming exactly what is missing.
- **prices add** appends a row; there is deliberately no edit or delete. `--effective-at`
  defaults to now; a past timestamp is allowed (backfill) with a stderr warning that existing
  request rows are not re-costed.
- **usage** prints one table per key: month, requests, cache hits, prompt/completion tokens,
  cost USD (4 decimals), plus a totals row. `--verify` recomputes `SUM(cost_usd)` per period and
  compares to `spent_usd`, printing `ok` per row or a discrepancy report; any mismatch exits 1.

## Output format

Tables are space-aligned with a header row and no box-drawing characters:

```
KEY          MONTH    REQUESTS  CACHE_HITS  PROMPT_TOK  COMPL_TOK  COST_USD
billing-app  2026-07      1842         311     2104332     398211   12.4183
support-bot  2026-07       412           0      388120      91230    2.1049
TOTAL                     2254         311     2492452     489441   14.5232
```

- `--json` emits a single JSON object (never NDJSON) with the same fields in snake_case, ISO-8601
  UTC timestamps, and costs as strings to avoid float drift in consumers.
- Empty results print an explicit line ("no usage recorded for 2026-07"), not an empty table.
- Errors print `error: <message>` to stderr, no traceback (tracebacks only with `LGW_DEBUG=1`).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including "nothing to do"). |
| 1 | Operation failed: not found, duplicate, validation, verify mismatch. |
| 2 | Usage or environment error: bad arguments, missing/unreadable database, bad `--month`. |

## Server-side UX notes

- Every response carries `X-LGW-Request-Id`; support requests quote it and the operator greps the
  structured log for it. `X-LGW-Provider` and `X-LGW-Cache` let clients see routing and cache
  behavior without parsing bodies.
- Error messages in the envelope are one sentence, actionable, and never echo prompt content:
  "Monthly budget exceeded for this key" rather than internal state dumps.
- SSE keeps OpenAI framing exactly (`data: {json}\n\n`, final `data: [DONE]\n\n`) so SDK stream
  parsers work unmodified; mid-stream failures send a final `data: {"error": ...}` event before
  closing, which raw-SSE consumers can distinguish from a clean finish.
