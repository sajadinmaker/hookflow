# HookFlow database

SQLite for local dev/tests, PostgreSQL in production (`postgresql+psycopg://...`).

Schema is owned by **Alembic**. `Base.metadata.create_all` was removed: creating
tables at boot means two replicas can race DDL, and it hides the fact that a
migration was never written.

```bash
alembic upgrade head          # deploy step
alembic check                 # fails if models and migrations have drifted
alembic downgrade base        # reverse the whole chain
```

`alembic.ini` deliberately carries no `sqlalchemy.url`. The URL is read from the
same `Settings` the application uses (`migrations/env.py`), so migrations and
the app cannot silently target different databases.

## Tables

- `tenants(id, name UNIQUE, created_at)`
- `api_keys(id, tenant_id → tenants, key_prefix UNIQUE, key_hash, label, created_at, last_used_at, revoked_at)`
- `endpoints(id, tenant_id → tenants NOT NULL, url, secret, rate_limit_per_minute, created_at)`
- `events(id, endpoint_id → endpoints, idempotency_key NULLABLE, payload TEXT raw JSON, created_at)`
- `deliveries(id, event_id → events, endpoint_id → endpoints, status, attempts,
  next_attempt_at, last_status_code, last_error, latency_ms, created_at, updated_at)`

`endpoints.secret` is **TEXT, not VARCHAR**. It holds a Fernet envelope
(`enc:v1:<token>`), which is ~147 characters for a generated secret. The column
was originally `VARCHAR(128)`, sized for a 64-char hex secret. SQLite does not
enforce VARCHAR length, so the full suite passed locally and PostgreSQL failed
with `StringDataRightTruncation` on the first run there. See migration
`5d66ae57cfaa`.

## Constraints & indexes (why they exist)

- `UNIQUE(endpoint_id, idempotency_key)` — closes the race where two concurrent
  ingests with the same key both pass the app-level "does it exist?" check.
  The loser gets `IntegrityError`, we catch it and return the winner
  (deduplicated=true). NULL keys are exempt in both SQLite and Postgres
  (multiple NULLs allowed).
- `INDEX deliveries(status, next_attempt_at)` — the worker's hot query
  (`WHERE status IN ('queued','retrying') AND (next_attempt_at IS NULL OR <= now)`)
  would otherwise full-scan on every 2s poll.
- `INDEX deliveries(endpoint_id, created_at)`, `INDEX deliveries(event_id)`,
  `INDEX events(endpoint_id, created_at)` — history pagination + filters.
- `INDEX deliveries(claim_token, claimed_at)` — the "unclaimed OR lease expired"
  half of the worker's claim predicate.
- `INDEX endpoints(tenant_id)`, `INDEX api_keys(tenant_id)` — tenant scoping on
  every authorised query.
- `UNIQUE api_keys(key_prefix)` — one key per prefix, so bearer lookup is a
  single-row index hit.

## Migration history

| Revision | Change |
|---|---|
| `68f2ffddab42` | initial schema: `endpoints`, `events`, `deliveries` |
| `5d66ae57cfaa` | widen `endpoints.secret` to TEXT for Fernet envelopes |
| `867af545e9dd` | worker claim lease: `deliveries.claim_token`, `claimed_at`, `ix_deliveries_claim` |
| `6c7e46e98ca0` | `tenants` + `api_keys`; `endpoints.tenant_id` NOT NULL with backfill |

### The tenant migration is hand-written

`alembic revision --autogenerate` produced `ADD COLUMN tenant_id NOT NULL` with
no default and no backfill, which fails against any database that already
contains endpoints. The committed revision instead adds the column nullable,
creates a `legacy` tenant, backfills existing endpoints onto it, then adds the
foreign key and enforces `NOT NULL`.

This was verified by migrating a database holding six pre-existing endpoints:
all six preserved, zero null `tenant_id` remaining, and `NOT NULL` confirmed
enforced by a rejected insert. Hand-written data migrations are the normal case
here — autogenerate infers DDL, never data.

## SQLite is not PostgreSQL

Two places where the local database is more permissive than production, both now
handled in `app/db.py` and covered by tests:

1. **Foreign keys are off by default in SQLite.** `make_engine` issues
   `PRAGMA foreign_keys=ON` per connection, so cascades and referential
   integrity behave as they do in PostgreSQL. Without this, local runs accept
   orphaned rows that production rejects.
2. **VARCHAR length is not enforced by SQLite.** This one already caused a real
   outage-shaped bug (above). The lesson generalises: a green SQLite suite is
   not evidence about PostgreSQL behaviour.

CI runs the full suite against PostgreSQL and Redis for these reasons.

## Adding a migration

```bash
alembic revision --autogenerate -m "what changed and why"
alembic upgrade head
python -m pytest
```

Review the generated file before committing — autogenerate will not add
constraints that are absent from the models, and it cannot infer data
backfills. `alembic check` in CI fails the build if a model change ships
without a matching revision.
