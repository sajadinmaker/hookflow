# HookFlow deployment

## Order of operations

Schema first, application second. The app does **not** create tables at boot.

```bash
alembic upgrade head
uvicorn app.main:app
```

Running the app against a database with no schema returns a `503` from
`/ready` with an explicit "run `alembic upgrade head`" message, rather than a
500 from the first query.

## Local (SQLite, no Redis)

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload      # /docs, /health, /ready, /metrics, /v1/stats
python -m app.worker               # separate terminal
```

## Full stack (Postgres + Redis)

```bash
docker compose -f docker/docker-compose.yml up --build
# app: :8000, postgres: :5432, redis: :6379
```

`DATABASE_URL=postgresql+psycopg://hookflow:hookflow@postgres:5432/hookflow`
`REDIS_URL=redis://redis:6379/0`

The Compose `app` and `worker` services do not run migrations. Add a one-shot
migration step (`alembic upgrade head`) before them; with multiple replicas,
letting each app container migrate at boot is the race this project removed on
purpose.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./hookflow.db` | `postgresql+psycopg://...` in production |
| `REDIS_URL` | *(empty)* | Empty disables Redis; in-process fallbacks are then **single-replica only** |
| `SECRET_ENCRYPTION_KEY` | *(empty)* | **Set this in production.** Empty uses a published dev key and `/ready` reports `secret_encryption: development-key` |
| `MAX_BODY_BYTES` | `262144` | |
| `DELIVERY_TIMEOUT_SECONDS` | `10.0` | |
| `MAX_ATTEMPTS` | `5` | Attempts before DLQ |
| `BACKOFF_SCHEDULE_SECONDS` | `[60,300,1800,7200,43200]` | Saturates at the last value |

## AWS minimal (what "I can operate this" means)

- RDS Postgres (db.t4g.micro to start) + ElastiCache Redis + EC2/ECS running
  `app` and `worker` as separate processes. Secrets in AWS Secrets Manager →
  env vars; `SECRET_ENCRYPTION_KEY` is the one that matters most. ALB terminates
  TLS.
- Migrations as a deploy task before the new version rolls out.
- CloudWatch: scrape `/metrics`, alert on `queue_depth` (via `/v1/stats`)
  growth + DLQ count + worker restarts.
- Backups: RDS automated snapshots. Redis holds only wake-up hints, so it is
  safe to lose.

## Operate

- **Liveness** `/health` — process is up.
- **Readiness** `/ready` — database reachable *and schema present*, Redis
  reachable if configured, plus the secret-encryption posture. Gate traffic on
  this.
- **Observe** `/metrics` (Prometheus text: ingest/dedupe/requeue counters) and
  `/v1/stats` (by status + queue depth).
- **Recover** DLQ → `POST /v1/deliveries/{id}/requeue`. Replaying a `success`
  is refused with `409`, since it would duplicate a side effect at the receiver.

## Scaling note

Run **one** worker replica. Due deliveries are selected without being claimed,
so two workers will both fetch and POST the same delivery. Fixing this properly
needs a claim/lease column or `SELECT ... FOR UPDATE SKIP LOCKED` — see the
limitations in the README before scaling the worker horizontally.
