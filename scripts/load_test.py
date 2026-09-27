"""Load probe for the HookFlow ingest path.

Measures the persist-first ingest cost: JSON serialise -> DB insert -> enqueue.

Schema is built with Alembic, not create_all, so the benchmark exercises the
same DDL production uses.

Usage:
    # local, no services
    python scripts/load_test.py --events 1000

    # against real engines
    docker compose -f docker/docker-compose.yml up -d postgres redis
    DATABASE_URL=postgresql+psycopg://hookflow:hookflow@localhost:5432/hookflow \
    REDIS_URL=redis://localhost:6379/0 \
    python scripts/load_test.py --events 5000 --payload-kb 1

Read the numbers for what they are: TestClient drives the ASGI app in-process,
so this measures application and database cost only. It is not a capacity
claim, and it excludes the worker's outbound HTTP. A server-level number
(uvicorn + concurrent clients) is a different measurement.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(int(len(sorted_values) * fraction), len(sorted_values) - 1)
    return sorted_values[index]


def run(events: int, payload_kb: int, database_url: str, redis_url: str) -> dict:
    # No URL, or an explicit sqlite://, means "use a throwaway file".
    ephemeral = not database_url or database_url.startswith("sqlite")
    if ephemeral:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        database_url = f"sqlite:///{tmp.name}"
        os.unlink(tmp.name)  # let Alembic create it

    os.environ["DATABASE_URL"] = database_url
    os.environ["REDIS_URL"] = redis_url

    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import Settings, get_settings
    from app.crypto import reset_secret_box_cache
    from app.migrations import upgrade_to_head

    upgrade_to_head(database_url)
    get_settings.cache_clear()
    reset_secret_box_cache()
    settings = Settings(database_url=database_url, redis_url=redis_url)
    main_module.init_state(settings)
    app = main_module.create_app(settings)

    payload = {"data": "x" * (payload_kb * 1024)}
    latencies: list[float] = []

    with TestClient(app) as client:
        created = client.post(
            "/v1/endpoints",
            json={"url": "https://example.com/wh", "rate_limit_per_minute": 10000},
        )
        assert created.status_code == 201, f"endpoint setup failed: {created.status_code} {created.text}"
        endpoint_id = created.json()["id"]

        # Warm the connection pool and the endpoint row so the first sample is
        # not paying one-off costs.
        client.post("/v1/events", json={"endpoint_id": endpoint_id, "payload": payload})

        start = time.monotonic()
        for _ in range(events):
            t0 = time.monotonic()
            r = client.post(
                "/v1/events",
                json={"endpoint_id": endpoint_id, "payload": payload},
            )
            assert r.status_code == 201, r.text
            latencies.append((time.monotonic() - t0) * 1000)
        total_s = time.monotonic() - start

    latencies.sort()
    dialect = "sqlite" if database_url.startswith("sqlite") else "postgresql"
    return {
        "events": events,
        "payload_kb": payload_kb,
        "total_s": round(total_s, 3),
        "rps": round(events / total_s, 1),
        "p50_ms": round(_percentile(latencies, 0.50), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
        "p99_ms": round(_percentile(latencies, 0.99), 2),
        "mean_ms": round(statistics.mean(latencies), 2),
        "engine": dialect,
        "redis": bool(redis_url),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--payload-kb", type=int, default=1)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="defaults to a throwaway SQLite file",
    )
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", ""))
    args = parser.parse_args()

    result = run(args.events, args.payload_kb, args.database_url, args.redis_url)
    print(
        f"ingest: {result['events']} events, {result['payload_kb']}KB payload | "
        f"{result['rps']} rps | p50 {result['p50_ms']}ms p95 {result['p95_ms']}ms "
        f"p99 {result['p99_ms']}ms mean {result['mean_ms']}ms "
        f"total {result['total_s']}s "
        f"({result['engine']}, redis={'on' if result['redis'] else 'off'}, "
        f"TestClient in-process, single worker)"
    )


if __name__ == "__main__":
    sys.exit(main())
