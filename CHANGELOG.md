# Changelog

## Unreleased

- Docs: design decisions and their reasons collected in `docs/decisions.md`; internal planning
  documents removed.
- Fix: a request swept for outliving its reservation TTL now still commits its actual cost to the
  budget and records it on the request row when it completes, as the architecture doc describes.

## 0.2.0 - 2026-11-09

- PII redaction: emails, phone numbers, and Luhn-valid card numbers become placeholders before a
  prompt leaves the gateway, and are restored in responses and streams.
- Opt-in response cache: exact match plus embedding similarity (OpenAI or Gemini embeddings),
  scoped per key and model, streamed hit replay, TTL and per-key cap eviction.
- A provider 401/403 fails over and is never relayed, so callers never see the operator's key.
- Docker image with a health check; CI on Python 3.12 and 3.13 plus an image smoke test.
- Coverage gate at 90 percent; tests for the admin CLI, startup validation, and the sweeper.

## 0.1.0

Initial release: OpenAI-compatible chat completions in front of OpenAI, Anthropic, and Gemini,
streaming with exact accounting, virtual keys, race-safe budgets, priority failover, versioned
prices, and the `lgw` admin CLI.
