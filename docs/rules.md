# Engineering Rules - llm-gateway

These rules are binding for every change in this repository and extend the workspace CLAUDE.md.

## Conventions

- **Layering**: Route handlers stay thin: parse, delegate, respond. Business logic lives in
  `app/services/`; provider wire-format knowledge lives only in `app/providers/`; SQL lives only
  in models and services. No provider-specific branches outside the adapters: the rest of the
  codebase speaks canonical OpenAI shapes exclusively.
- **Async discipline**: The request path is fully async; no blocking I/O (no `requests`, no
  synchronous file reads) inside handlers or services. SQLAlchemy sessions are short-lived and
  scoped per operation; never hold a session open across an upstream call or a stream.
- **Preferred libraries**: Only what `docs/architecture.md` names: FastAPI, Pydantic, SQLAlchemy,
  Alembic, httpx, redis-py (optional import), pytest, pytest-asyncio, ruff, black. No numpy, no
  tiktoken, no provider SDKs (`openai`, `anthropic`, `google-genai`): upstream calls are plain
  httpx against documented wire formats, which keeps translation explicit and testable.
- **What to avoid**: No `os.environ` reads outside `app/config.py`; no module-level singletons
  holding request state; no `dict` passing where a Pydantic model or dataclass exists; no
  `except Exception: pass`; no threads (asyncio tasks only).
- **Naming**: Modules and functions snake_case; Pydantic models and SQLAlchemy classes
  PascalCase; tables plural snake_case; enum values lowercase strings as listed in
  `docs/architecture.md`. Placeholder format, header names, and error codes are contract items:
  never rename them.
- **Commit format**: Conventional Commits, short imperative subject, lowercase after the prefix,
  e.g. `feat(budget): add stale reservation sweeper`. One commit per feature/task in the order
  listed in `docs/phases.md`; never batch features, never fragment one small feature.
- **Dependencies pinned**: Exact versions in `pyproject.toml`, `uv.lock` committed. Any
  dependency change is its own commit and needs owner approval first.
- **Migrations**: Every schema change is an Alembic migration; applied migrations are never
  edited: add a new one. Model changes ship in the same commit as their migration.
- **State transitions**: `requests.status` and `reservations.state` may only change along the
  transitions in `docs/architecture.md`, always via conditional UPDATEs that name the expected
  prior state. Never set a state unconditionally.

## Error handling & logging

- **Every external call handles failure**: upstream HTTP (timeout, connect, 5xx, malformed
  body), the embedding call, Redis, and every DB write. An upstream attempt that throws still
  writes its `attempts` row before failover proceeds.
- **The endpoint never 500s on bad input**: unknown key, unknown model, unsupported params,
  oversize body, and over-budget each map to their documented status and envelope. A genuine bug
  returns 500 `server_error` with details in logs only.
- **One error envelope everywhere** (see `docs/api-contracts.md`): the OpenAI-compatible
  `{"error": {"message", "type", "code"}}` shape for every JSON error, including mid-stream SSE
  error events. Never a bare string, never a stack trace in a response.
- **Structured JSON logs from day one** with consistent dotted event keys: `request.completed`,
  `request.failed`, `request.cut_off`, `auth.denied`, `budget.rejected`, `budget.swept`,
  `failover.attempt_failed`, `cache.hit`, `cache.store`, `cache.redis_degraded`,
  `redaction.applied` (count only), `prices.missing`, `startup.validated`. Every request-path log
  line carries the request id.
- **Never log**: prompt or response bodies (unless `LGW_LOG_BODIES`, and then redacted only),
  PII values or placeholder maps, raw virtual keys, provider API keys, or full upstream request
  payloads. Upstream error body excerpts are capped at 512 chars and logged only at WARNING.

## Security

- **No hardcoded secrets**: provider keys and config live in `.env` (git-ignored);
  `.env.example` carries dummies and every variable. Virtual keys are stored only as SHA-256
  hashes plus last-4; the raw key is printed exactly once by the CLI.
- **Auth on everything**: every route except `GET /health` requires a valid, active virtual key.
  Revocation is checked on every request (no key caching that outlives a revoke).
- **Validate all input server-side** via Pydantic with `extra="forbid"` on the request schema so
  unsupported OpenAI params fail loudly instead of silently dropping; enforce `LGW_MAX_BODY_KB`
  before parsing.
- **Queries**: SQLAlchemy parameter binding only; the two raw SQL statements allowed are the
  budget reserve/commit conditional UPDATEs, which use bound parameters.
- **Outbound calls** go only to the three pinned provider base URLs from config; the gateway
  never fetches URLs derived from request content (no SSRF surface).
- **Privacy is a hard rule**: redaction always runs; the placeholder map never persists; cache
  stores placeholders, not PII; audit rows carry counts and metadata only. Any change that
  weakens this needs owner sign-off, not a code review comment.

## Simplicity / YAGNI-KISS

- Build only what the current phase requires. No speculative provider params, no per-route
  weights, no config toggles beyond the documented `LGW_*` set.
- The adapter interface is justified by three concrete providers; nothing else warrants an
  interface or wrapper class in v1 without approval.
- Prefer the boring mechanism: one attempt per route, one conditional UPDATE for the budget,
  brute-force cosine over a bounded scan. If a solution exceeds ~150 lines, pause and justify it.

## Code style

- Type hints on every function signature; `mypy`-clean is not required but obvious type errors
  are defects. Ruff and black own formatting; do not hand-format against them.
- Comments are sparse and explain why, not what. The restoration tail buffer, the reservation
  race resolution, and the Luhn check deserve docstrings; getters do not.
- No emoji anywhere in code, comments, commits, or docs. No AI or authorship attribution
  anywhere, including commit trailers.

## Boundaries - never do without asking the owner first

- No wholesale delete/rewrite of working files; targeted edits, destructive changes flagged first.
- Do not change `docs/PRD.md` or `docs/architecture.md` without flagging the change and its
  reason and getting sign-off.
- No new dependency without approval: propose what, why, version, and size, then wait.
- Stop after two failed fix attempts on the same problem and report instead of thrashing.
- Any mid-phase request not in the PRD gets classified with the owner as current phase, new
  phase, or Backlog in `docs/phases.md`. Never silently absorb scope.
