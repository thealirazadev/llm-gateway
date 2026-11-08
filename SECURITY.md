# Security

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: **Security > Report a vulnerability** on this
repository. Do not open a public issue. You will get a reply within five working days.

Supported: the `main` branch.

## Security model

- **Virtual keys.** Callers never see provider keys. Gateway keys are shown once and stored as
  SHA-256 hashes; lookup is by hash, so there is no secret comparison to time.
- **Provider credentials stay private.** Upstream base URLs are pinned in code, never derived from
  a request. When a provider rejects the gateway's key (401/403), the request fails over and the
  provider's message, which can quote part of the key, is logged for the operator and never
  returned to the caller.
- **PII redaction.** Emails, phone numbers, and Luhn-valid card numbers are replaced before a
  prompt reaches a provider, the cache, or an embedding call. The placeholder map exists only in
  request memory; the database stores a count.
- **Spend limits.** Monthly budgets use atomic reservations, so concurrent requests cannot race
  past a cap.
- **Cache isolation.** Cache entries are scoped to (key, model), hold redacted text only, and a
  cached answer naming a placeholder the current request cannot restore is never served.
- **Input limits.** Bodies over `LGW_MAX_BODY_KB` are rejected before parsing; unsupported
  parameters are rejected with a precise error.
- **Logs.** Structured JSON with request ids; no prompt bodies unless `LGW_LOG_BODIES` is set,
  and then only redacted text.
- **Deployment.** The server does not terminate TLS: put it behind an HTTPS reverse proxy. The
  Docker image runs as a non-root user and keeps SQLite on the `/data` volume.
