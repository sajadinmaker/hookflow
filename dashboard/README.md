# HookFlow dashboard

Operator UI for inspecting deliveries, replaying from the dead letter queue,
and reviewing API keys.

## The security constraint that shapes this app

The dashboard holds a tenant API key. That key is read from the server
environment in `lib/hookflow.ts` and is **never** exposed to the browser:

- every data page is a server component fetching with `cache: "no-store"`, so
  the key stays in the Node process
- replay is a **server action** (`app/actions.ts`), not a client-side fetch, so
  the key is never serialised into the browser bundle
- the one client component (`ReplayButton`) receives only a delivery id

If a page ever needs `NEXT_PUBLIC_*` access to these values, the key is already
leaked — that is the check to make before adding a client component.

## Running

```bash
# 1. the API, with a tenant and key
HOOKFLOW_ADMIN_TOKEN=dev-token \
DATABASE_URL=postgresql+psycopg://hookflow:hookflow@localhost:5432/hookflow \
  uvicorn app.main:app --port 8000

curl -X POST localhost:8000/v1/admin/tenants \
  -H 'X-Admin-Token: dev-token' -H 'Content-Type: application/json' \
  -d '{"name":"acme"}'

# ...then mint a key for that tenant id and copy the one-time plaintext

# 2. this dashboard
cp .env.example .env.local   # fill in HOOKFLOW_API_KEY
npm install
npm run dev                  # http://localhost:3000
```

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `HOOKFLOW_API_URL` | no | API base URL (default `http://localhost:8000`) |
| `HOOKFLOW_API_KEY` | **yes** | Tenant API key. Server-side only. |
| `HOOKFLOW_ADMIN_TOKEN` | no | Enables the API keys view |
| `HOOKFLOW_TENANT_ID` | no | Which tenant the keys view lists |

## Pages

| Route | Shows |
|---|---|
| `/` | Queue depth, delivery outcomes, p95 latency, readiness, recent deliveries |
| `/endpoints` | Registered destinations and rate limits |
| `/deliveries` | Filterable by status, with replay |
| `/dlq` | Exhausted deliveries awaiting replay |
| `/keys` | Key prefixes, last use, revocation state |
| `/metrics` | Parsed Prometheus counters plus raw exposition |

## Checks

```bash
npm run typecheck
npm run build
```
