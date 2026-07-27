# Launch Checklist - llm-gateway

Work top to bottom before pointing production traffic at an instance. Nothing is checked until
verified in the target environment.

## Environment & configuration

- [ ] Production `.env` created from `.env.example` with real values; `.env` git-ignored.
- [ ] Provider API keys present for every provider referenced by `lgw routes list`; no unused
      provider keys configured.
- [ ] `DATABASE_URL` points at a persistent volume; WAL files included in the backup path.
- [ ] `LGW_*` settings reviewed for this deployment (timeouts, reservation TTL, cache tuning,
      body limit); `LGW_LOG_BODIES` confirmed false.
- [ ] Startup validation passes: every routed model has a price row and a credentialed provider.

## Data & money

- [ ] Prices entered for every routed (provider, model) and spot-checked against the providers'
      current published pricing.
- [ ] Every key created with an explicit budget unless unlimited is intentional and recorded.
- [ ] `lgw usage --verify` returns ok on the fresh instance and again after a day of traffic.
- [ ] Database backup scheduled and a restore tested once (keys, budgets, audit, cache).

## Security & privacy

- [ ] Gateway reachable only over HTTPS (reverse proxy terminates TLS); plain HTTP refused or
      redirected.
- [ ] A test key revoked and confirmed 401 on the next request.
- [ ] Redaction verified against the live instance: a prompt with a real-format email/phone/card
      arrives at the provider dashboard logs redacted (check one provider's request log).
- [ ] Database and log spot-check: no prompt bodies, no PII values, no raw keys anywhere.
- [ ] `GET /health` is the only unauthenticated route (probe the rest without a key).

## Reliability

- [ ] Failover drill: block route 1 at the network level, confirm traffic succeeds via route 2
      and attempts are audited.
- [ ] Kill-the-process drill mid-stream: on restart the boot sweep releases held reservations and
      `usage --verify` still reconciles.
- [ ] Client-disconnect drill with curl: row `cut_off`, cost committed.
- [ ] Redis (if configured) stopped and started under traffic: warnings logged, zero request
      failures.
- [ ] Process supervised (systemd unit or equivalent) with restart-on-failure and graceful stop
      (SIGTERM drain observed).

## Quality gates

- [ ] CI green on the deployed commit; `ruff check`, `black --check`, `pytest` clean locally.
- [ ] `uv.lock` committed and matching the deployed environment (`uv sync --frozen`).
- [ ] README instructions executed verbatim on a clean machine reproduce a working instance.
- [ ] Structured logs verified in production: documented event keys only, request ids present,
      zero ERROR lines during the drills above.
