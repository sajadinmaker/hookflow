# HookFlow

Reliable webhook and event delivery infrastructure.

Accept an event durably, then deliver it with retries, signatures, and a
dead-letter state — so a flaky consumer degrades into a queued retry instead of
a lost event.

## Problem

Webhook receivers time out, return 5xx, and go away mid-deploy. Producers that
POST inline lose events on any transient failure, and have no way to replay
them or prove to the receiver that a payload was not tampered with.

## Approach

Persist-first ingest, asynchronous signed delivery, exponential backoff,
idempotency keys, per-endpoint rate limits, and a dead-letter state with full
delivery history and replay.

```text
Client
  ↓
FastAPI  ── persist Event + Delivery (one durable transaction)
  ↓
PostgreSQL          ← source of truth
  ↓
Redis list          ← wake-up hint only (LPUSH after commit)
  ↓
Worker
  ↓
Endpoint  (X-Hookflow-Signature: sha256=HMAC(secret, body))
```

## Design decisions

**Persist before enqueue.** An event is only acknowledged after the Event and
Delivery rows are committed. Redis is pushed to *afterwards*, purely as a
wake-up hint — the worker also polls due rows, so losing a Redis message costs
latency, never an event. This is why Redis can be optional without correctness
becoming conditional on it.

**At-least-once, made safe by idempotency keys.** Exactly-once delivery is not
achievable over HTTP to a third party. Instead, callers may send an
`Idempotency-Key`; a `UNIQUE (endpoint_id, idempotency_key)` constraint makes
duplicate acceptance impossible at the database level, so two concurrent
ingests cannot both insert. The loser of that race catches the `IntegrityError`
and returns the winner's event rather than a 500.

**Secrets encrypted at rest, not hashed.** A webhook secret is *shared*: the
receiver verifies a signature we produce, so the server must reproduce the
exact bytes at signing time. A one-way hash can verify an inbound credential but
can never sign an outbound payload, so hashing would break delivery outright.
Stored value is a Fernet envelope (`enc:v1:<token>`); plaintext exists only in
memory for the duration of one signing call. Key access sits behind a
`KeyProvider` with a single method, so AWS KMS or Vault can replace it without
touching call sites or stored data. A generated secret is returned exactly once,
at creation, because otherwise the receiver could never verify anything.

**Schema owned by migrations.** Tables are created by `alembic upgrade head` as
a deploy step, never implicitly at boot. Booting and mutating schema in the same
process is how replicas end up racing DDL, and it hides the fact that a
migration was never written. `alembic check` runs in CI, so a model change
without a matching migration fails the build.

## Correctness testing

The test suite is aimed at the failure modes that are easy to claim and easy to
leave untested:

- **The idempotency race is mutation-verified.** Removing the unique
  constraint from the migration makes the concurrency test fail; it is not a
  test that passes because threads happened to serialise.
- **A secret is proven to work end to end**: the value disclosed at creation is
  used to recompute the HMAC the worker actually emitted.
- Transport errors are distinguished from HTTP statuses, and a 4xx/5xx and a
  connection failure follow different paths.
- Deleting an endpoint mid-flight, DLQ exhaustion, and backoff saturation.

`python -m pytest` — **48 tests**, 91% coverage. The suite runs green on three
configurations, because the default one hides real bugs:

```bash
python -m pytest                                                  # SQLite
REDIS_URL=redis://localhost:6379/0 python -m pytest               # + Redis
HOOKFLOW_TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest   # PostgreSQL
```

Running against PostgreSQL immediately paid for itself: encrypted secrets are
~147 characters, which overflowed `VARCHAR(128)`. **SQLite does not enforce
VARCHAR length, so the entire suite passed locally and endpoint registration
would have failed on the first production deploy.** CI now runs PostgreSQL and
Redis for exactly this reason. `app/db.py` likewise enables
`PRAGMA foreign_keys` on SQLite, which is off by default — otherwise local runs
silently accept rows PostgreSQL would reject.

## Measured performance

Numbers are measured, not estimated. Reproduce with `scripts/load_test.py`;
see `docs/performance.md` for method and caveats.

| Engine | RPS | p50 | p95 | p99 |
|---|---|---|---|---|
| SQLite | 112.3 | 7.5ms | 18.9ms | 24.5ms |
| PostgreSQL 15 | 52.1–53.9 | 17.1–18.5ms | 34.0ms | 40.0–41.6ms |
| PostgreSQL + Redis | 48.3–55.3 | 16.5–19.9ms | 34.8–35.3ms | 40.8–42.7ms |

The interesting result is that PostgreSQL is ~2x *slower* per request than
SQLite here, and that enabling Redis costs almost nothing. Ingest performs one
durable commit per event, so with default `synchronous_commit=on` the cost is
one `fsync` per request; Redis is a single `LPUSH` after that commit. The
bottleneck is commit latency, and the fix is group commit, not more queueing
machinery.

These are sequential in-process measurements of per-request latency. They are
**not** a capacity claim and exclude HTTP server, concurrency, and worker
delivery.

## Running locally

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env

alembic upgrade head                 # schema is a deploy step
uvicorn app.main:app --reload        # /docs /health /ready /metrics
python -m app.worker                 # delivery worker, separate terminal

docker compose -f docker/docker-compose.yml up --build   # full stack
```

Set `SECRET_ENCRYPTION_KEY` in any real deployment. With it unset, `/ready`
reports `secret_encryption: development-key` so the mistake is visible rather
than silent.

## Limitations

Stated plainly, because the interesting ones are the reason for the next steps:

- **A single worker replica only.** `fetch_due_deliveries` selects due rows
  without claiming them — no `FOR UPDATE SKIP LOCKED`, no lease column. Two
  workers polling concurrently will both fetch the same delivery and both POST
  it. This is a real double-delivery bug and is the most important thing to fix
  before scaling out; the durable fix is a claim/lease column (or
  `SKIP LOCKED`), not a queue library.
- **No multi-tenant auth.** Endpoints are unauthenticated; possession of the
  endpoint id is the only check. No API keys.
- Rate-limit memory fallback is per-process, so it is single-replica only.
- Backoff schedule is fixed at config level, not per endpoint.
- No TLS in Compose; terminate at a load balancer.

## Next

1. Multi-worker claim safety (lease column or `SKIP LOCKED`) — correctness.
2. Tenants and API-key auth — required before this is multi-tenant.
3. Concurrent-load and worker-throughput benchmarks.
4. Group commit on the ingest path, which the measurements above point at.
