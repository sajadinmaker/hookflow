"""Delivery worker: sign, POST with timeout, retry with backoff, or DLQ."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import Settings
from .crypto import get_secret_box
from .db import Delivery, Endpoint, utcnow
from .queue import DeliveryQueue
from .ratelimit import RateLimiter
from .security import SIGNATURE_HEADER, sign_payload


def backoff_delay_seconds(attempt: int, schedule: list[int]) -> int:
    """Delay after a failed attempt (1-indexed). Past the schedule, repeat last."""
    if attempt < 1:
        return schedule[0]
    if attempt <= len(schedule):
        return schedule[attempt - 1]
    return schedule[-1]


def deliver_once(
    body: bytes,
    url: str,
    secret: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> tuple[int | None, str | None, int]:
    """POST the payload once. Returns (status_code, error, latency_ms)."""
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_payload(secret, body),
        "User-Agent": "hookflow/0.1.0",
    }
    start = time.monotonic()
    try:
        if client is not None:
            response = client.post(url, content=body, headers=headers)
            status = response.status_code
        else:
            with httpx.Client(timeout=timeout_seconds) as http:
                response = http.post(url, content=body, headers=headers)
                status = response.status_code
        latency_ms = int((time.monotonic() - start) * 1000)
        if 200 <= status < 300:
            return status, None, latency_ms
        return status, f"unexpected status {status}", latency_ms
    except Exception as exc:  # timeouts, DNS, connection errors -> retryable
        latency_ms = int((time.monotonic() - start) * 1000)
        return None, f"{type(exc).__name__}: {exc}", latency_ms


def settle_delivery(
    session,
    settings: Settings,
    delivery: Delivery,
    endpoint: Endpoint,
    body: bytes,
    client: httpx.Client | None = None,
) -> Delivery:
    delivery.attempts += 1
    # The stored secret is ciphertext. It is decrypted here, at the only moment
    # the plaintext is genuinely required, and never written back or logged.
    plaintext_secret = get_secret_box(settings.secret_encryption_key).decrypt(endpoint.secret)
    status_code, error, latency_ms = deliver_once(
        body, endpoint.url, plaintext_secret, settings.delivery_timeout_seconds, client
    )
    delivery.last_status_code = status_code
    delivery.last_error = error
    delivery.latency_ms = latency_ms
    if error is None:
        delivery.status = "success"
        delivery.next_attempt_at = None
    elif delivery.attempts >= settings.max_attempts:
        delivery.status = "dlq"
        delivery.next_attempt_at = None
    else:
        delivery.status = "retrying"
        delay = backoff_delay_seconds(delivery.attempts, settings.backoff_schedule_seconds)
        delivery.next_attempt_at = utcnow() + timedelta(seconds=delay)
    delivery.updated_at = utcnow()
    session.add(delivery)
    session.commit()
    return delivery


def fetch_due_deliveries(session, limit: int = 20) -> list[Delivery]:
    now = utcnow()
    return (
        session.query(Delivery)
        .filter(Delivery.status.in_(["queued", "retrying"]))
        .filter((Delivery.next_attempt_at.is_(None)) | (Delivery.next_attempt_at <= now))
        .order_by(Delivery.created_at)
        .limit(limit)
        .all()
    )


def process_due_deliveries(
    session,
    settings: Settings,
    rate_limiter: RateLimiter,
    client: httpx.Client | None = None,
    limit: int = 20,
) -> dict[str, int]:
    """One worker pass. Returns counts by outcome. Rate-limited endpoints are skipped."""
    stats = {"success": 0, "retrying": 0, "dlq": 0, "rate_limited": 0}
    for delivery in fetch_due_deliveries(session, limit):
        endpoint = session.get(Endpoint, delivery.endpoint_id)
        if endpoint is None:
            delivery.status = "dlq"
            delivery.last_error = "endpoint deleted"
            session.add(delivery)
            session.commit()
            stats["dlq"] += 1
            continue
        if not rate_limiter.allow(endpoint.id, endpoint.rate_limit_per_minute):
            stats["rate_limited"] += 1
            continue
        event_body = delivery.event.payload.encode() if delivery.event else b"{}"
        settled = settle_delivery(session, settings, delivery, endpoint, event_body, client)
        stats[settled.status if settled.status in stats else "retrying"] += 1
    return stats


def run_worker_forever(session_factory, settings: Settings, poll_seconds: float = 2.0) -> None:
    """Long-running worker loop (used by `python -m app.worker`)."""
    import time as _time

    rate_limiter = RateLimiter(settings.redis_url)
    queue = DeliveryQueue(settings.redis_url)
    while True:
        queued_id = queue.dequeue(timeout=1)
        session = session_factory()
        try:
            if queued_id:
                delivery = session.get(Delivery, queued_id)
                if delivery is not None and delivery.status in ("queued", "retrying"):
                    endpoint = session.get(Endpoint, delivery.endpoint_id)
                    if endpoint and rate_limiter.allow(
                        endpoint.id, endpoint.rate_limit_per_minute
                    ):
                        body = delivery.event.payload.encode() if delivery.event else b"{}"
                        settle_delivery(session, settings, delivery, endpoint, body)
            process_due_deliveries(session, settings, rate_limiter)
        finally:
            session.close()
        _time.sleep(poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    from .config import get_settings
    from .db import make_engine, make_session_factory

    _settings = get_settings()
    _engine = make_engine(_settings.database_url)
    run_worker_forever(make_session_factory(_engine), _settings)
