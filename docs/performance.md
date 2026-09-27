# HookFlow performance

All numbers below were **measured on this machine**, not estimated. Reproduce
with:

```bash
docker compose -f docker/docker-compose.yml up -d postgres redis

# SQLite
python scripts/load_test.py --events 500

# PostgreSQL
python scripts/load_test.py --events 2000 \
  --database-url postgresql+psycopg://hookflow:hookflow@localhost:5432/hookflow

# PostgreSQL + Redis enqueue
python scripts/load_test.py --events 2000 \
  --database-url postgresql+psycopg://hookflow:hookflow@localhost:5432/hookflow \
  --redis-url redis://localhost:6379/0
```

Measured 2026-09-27, after the Alembic and secret-encryption changes.

## Method, and what it excludes

`scripts/load_test.py` drives the ASGI app in-process through
`fastapi.testclient`. It therefore measures **application logic + database
cost** and nothing else. Specifically it does *not* include:

- HTTP server or socket overhead (no uvicorn, no network)
- concurrency (single-threaded sequential requests, so no lock contention)
- the delivery worker's outbound HTTP
- multi-replica behaviour

Because requests are sequential, the figures describe per-request latency on
an idle system, **not** throughput under load. Treat the RPS column as
"requests per second of one client waiting for each response", not as a
capacity number.

## Results

1KB payload, sequential single client:

| Engine | Events | RPS | p50 | p95 | p99 | mean |
|---|---|---|---|---|---|---|
| SQLite (file) | 500 | 112.3 | 7.5ms | 18.9ms | 24.5ms | 8.9ms |
| PostgreSQL 15 | 2000 | 52.1 | 18.5ms | 34.0ms | 40.0ms | 19.2ms |
| PostgreSQL 15 | 2000 | 53.9 | 17.1ms | 34.1ms | 41.6ms | 18.6ms |
| PostgreSQL + Redis | 1500 | 48.3 | 19.9ms | 34.8ms | 42.7ms | 20.7ms |
| PostgreSQL + Redis | 1500 | 55.3 | 16.5ms | 35.3ms | 40.8ms | 18.1ms |

## What the numbers actually say

**PostgreSQL is roughly 2x slower per request than SQLite here, and the cause is
the commit, not the query.** Ingest performs one durable transaction per event
(Event row + Delivery row). On PostgreSQL with default `synchronous_commit=on`,
that is one `fsync` per request, which is where the ~15ms goes. SQLite is
faster only because it is a local file with a much cheaper commit path — that
is an artefact of the test setup, not a claim that SQLite is the better
production database.

**Enabling Redis costs almost nothing.** 48–55 rps with Redis versus 52–54
without: the `LPUSH` happens *after* the commit and is dwarfed by the `fsync`.
This is the persist-first design paying off — Redis is a wake-up hint, and the
database remains the source of truth, so its absence degrades latency rather
than correctness.

**The real bottleneck is durable commit latency, and the fix is batching.**
Options, in increasing order of effort:

1. `synchronous_commit = off` — trades durability on crash for throughput.
   Acceptable only if losing the last few events is tolerable.
2. Group commit — batch N accepted events into one transaction. Preserves
   durability per batch, and is the correct answer for a webhook receiver.
3. Asynchronous acceptance — return `202` after an in-memory enqueue and let a
   writer persist. Changes the delivery guarantee and needs care: it must be
   paired with the DLQ so nothing is silently lost.

## Not yet measured

Do not cite numbers for any of these until they are actually run:

- Concurrent ingest (N parallel clients) — the sequential numbers above say
  nothing about behaviour under contention, which is the interesting case.
- Worker delivery throughput against a stub receiver (success/5xx/timeout mix).
- End-to-end p95: ingest → signed POST → receiver acknowledgement.
- p99/p999 under sustained load. Two runs is not a distribution.
