# HookFlow — Product Documentation

> User-serving product guide (technical deep docs already in `docs/`).
> Codebase: `/home/sajad/Projects/portfolio/hookflow` | Stack: FastAPI + PostgreSQL + Redis + Next.js dashboard | Tests: 79, 93% cov

## 1. What this product is

Reliable webhook/event delivery: persist-first ingest, async HMAC-signed delivery with exponential backoff, idempotency keys, per-endpoint rate limits, DLQ + replay, full delivery history. Redis is a wake-up hint only — losing it costs latency, never events.

**Who it's for:** backend teams whose webhook receivers time out / 5xx / redeploy mid-delivery and need provable, replayable delivery.

## 2. How users use it

1. Provision tenant + API key (admin): `POST /v1/admin/tenants`, `POST /v1/admin/tenants/{id}/keys` (`hf_<prefix>_<secret>`, secret shown once)
2. Register endpoint: `POST /v1/endpoints { url }` → secret returned once (Fernet `enc:v1:` at rest)
3. Ingest: `POST /v1/events` + `Idempotency-Key` → Event + Delivery committed, Redis LPUSH hint
4. Worker (`python -m app.worker`) claims via `UPDATE ... SKIP LOCKED` + lease, POSTs with `X-Hookflow-Signature: sha256=HMAC(secret, body)`
5. Operate: `GET /v1/deliveries?status=`, `POST /v1/deliveries/{id}/requeue` (refuses `success` 409), `GET /v1/stats`, dashboard (Next.js server-components, key never in browser)

Full routes + curl: see `README.md` API section.

## 3. Serve it

```bash
cp .env.example .env  # SECRET_ENCRYPTION_KEY required in prod
docker compose -f docker/docker-compose.yml up --build  # :8000 / :5432 / :6379
alembic upgrade head  # schema is a deploy step, never at boot
uvicorn app.main:app --reload & python -m app.worker
python -m pytest  # 79 tests; PG: HOOKFLOW_TEST_DATABASE_URL=... ; Redis: REDIS_URL=...
```

Ops: `/health`, `/ready` (flags dev encryption key), `/metrics` (unauth — put behind network policy); `JOB_RETENTION`, `OUTPUT_MAX_GB`; TLS at LB (none in Compose).

## 4. Gaps → roadmap (from repo)

Per-tenant API quotas + self-service signup/billing; key expiry/rotation; multi-operator admin identity/audit; group commit (PG ~2x slower than SQLite on sequential ingest: 52 vs 112 RPS — bottleneck is per-event fsync); concurrent/worker-throughput benchmarks.
