# HookFlow architecture

```text
Client
  ↓ POST /v1/events (validate, Idempotency-Key)
FastAPI (register endpoints, ingest, history)
  ↓ persist first
PostgreSQL (endpoints, events, deliveries) — SQLite for local/tests
  ↓ notify
Redis list (fast wake-up; optional) ──→ Worker (poll due rows regardless)
  ↓ sign + POST + timeout
Customer webhook (verifies X-Hookflow-Signature)
  ↓
Delivery history (/v1/deliveries) + replay (POST /v1/deliveries/:id/requeue)
  + /health + /ready + /metrics + /v1/stats (queue depth by status)
```

Key decisions:
1. Persist-first ingest: an accepted event is never lost even if Redis drops the wake-up, because the worker polls due DB rows.
2. At-least-once delivery: retries with backoff (60s, 5m, 30m, 2h, 12h; 5 attempts) then DLQ with replay endpoint (attempts reset, success not requeueable).
3. Idempotency keys scoped per endpoint (body or `Idempotency-Key` header) dedupe retries safely; `UNIQUE(endpoint_id, idempotency_key)` + `IntegrityError` catch closes the concurrent-insert race.
4. HMAC-SHA256 per-endpoint secret; verification docs in README.
5. Fixed-window per-endpoint rate limits; Redis when configured, memory fallback single-replica only.
6. Observability without new deps: `X-Request-ID` middleware, Prometheus-text `/metrics`, `/v1/stats` for queue-depth alerts; worker poll indexed on `(status, next_attempt_at)`.
7. Schema owned by Alembic, applied as a deploy step — never `create_all` at boot, so replicas cannot race DDL and a missing migration cannot hide.
8. Webhook secrets encrypted at rest (Fernet envelope) behind a `KeyProvider` seam, plaintext present only while signing. Encryption rather than hashing is forced by the fact that the server must reproduce the secret to sign; see `docs/security.md`.

7. Workers claim what they deliver (lease + atomic claim query), so replicas
   do not double-deliver. Redis + PostgreSQL required for replica-safe
   rate limiting.
8. API-key auth resolves each request to exactly one tenant; queries are
   filtered by `tenant_id` and cross-tenant access answers 404, not 403.

Known boundaries: the rate limiter is per-process without Redis, and API keys
cannot expire. See the README limitations.
