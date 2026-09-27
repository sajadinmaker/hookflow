"""Delivery worker: sign, POST with timeout, retry with backoff, or DLQ."""

from __future__ import annotations

import time
import uuid
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


def claim_due_deliveries(
    session,
    token: str,
    limit: int = 20,
    lease_seconds: float = 60.0,
) -> list[Delivery]:
    """Atomically take ownership of up to `limit` due deliveries.

    Safety comes from the fact that this is a single UPDATE whose subquery
    excludes rows that are already claimed with a live lease. The database
    serialises the two racing workers, and the loser's subquery is re-evaluated
    against the winner's committed rows, so it simply finds fewer candidates.

    A plain SELECT-then-UPDATE would not be safe: both workers could read the
    same free row before either wrote. On PostgreSQL the subquery additionally
    uses FOR UPDATE SKIP LOCKED so N workers do not queue behind each other;
    SQLite has no row locks, but serialises writers anyway.
    """
    now = utcnow()
    stale_before = now - timedelta(seconds=lease_seconds)

    due = (
        session.query(Delivery.id)
        .filter(Delivery.status.in_(["queued", "retrying"]))
        .filter((Delivery.next_attempt_at.is_(None)) | (Delivery.next_attempt_at <= now))
        .filter(
            (Delivery.claim_token.is_(None)) | (Delivery.claimed_at < stale_before)
        )
        .order_by(Delivery.created_at)
        .limit(limit)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        due = due.with_for_update(skip_locked=True)

    session.query(Delivery).filter(Delivery.id.in_(due)).update(
        {Delivery.claim_token: token, Delivery.claimed_at: now},
        synchronize_session=False,
    )
    session.commit()

    return (
        session.query(Delivery)
        .filter(Delivery.claim_token == token)
        .order_by(Delivery.created_at)
        .all()
    )


def release_claim(session, delivery: Delivery) -> None:
    """Drop the lease so the row is immediately claimable again."""
    delivery.claim_token = None
    delivery.claimed_at = None
    session.add(delivery)


def release_expired_claims(session, lease_seconds: float = 60.0) -> int:
    """Free rows held by a worker that died mid-delivery. Returns rows freed."""
    stale_before = utcnow() - timedelta(seconds=lease_seconds)
    freed = (
        session.query(Delivery)
        .filter(Delivery.claim_token.isnot(None))
        .filter(Delivery.claimed_at < stale_before)
        .update(
            {Delivery.claim_token: None, Delivery.claimed_at: None},
            synchronize_session=False,
        )
    )
    session.commit()
    return freed


def process_due_deliveries(
    session,
    settings: Settings,
    rate_limiter: RateLimiter,
    client: httpx.Client | None = None,
    limit: int = 20,
    claim_token: str | None = None,
) -> dict[str, int]:
    """One worker pass. Returns counts by outcome. Rate-limited endpoints are skipped.

    Pass `claim_token` to claim the batch exclusively. Omit it for the
    single-worker case, where claiming would be pure overhead.
    """
    stats = {"success": 0, "retrying": 0, "dlq": 0, "rate_limited": 0}
    token = claim_token or uuid.uuid4().hex
    exclusive = claim_token is not None
    lease_seconds = settings.claim_lease_seconds

    if exclusive:
        release_expired_claims(session, lease_seconds)
        batch = claim_due_deliveries(session, token, limit, lease_seconds)
    else:
        batch = fetch_due_deliveries(session, limit)

    for delivery in batch:
        endpoint = session.get(Endpoint, delivery.endpoint_id)
        if endpoint is None:
            delivery.status = "dlq"
            delivery.last_error = "endpoint deleted"
            if exclusive:
                release_claim(session, delivery)
            session.add(delivery)
            session.commit()
            stats["dlq"] += 1
            continue
        if not rate_limiter.allow(endpoint.id, endpoint.rate_limit_per_minute):
            # Leave the claim in place: the row is rate-limited, not finished,
            # and the next pass should pick it up.
            if exclusive:
                release_claim(session, delivery)
                session.commit()
            stats["rate_limited"] += 1
            continue
        event_body = delivery.event.payload.encode() if delivery.event else b"{}"
        if exclusive:
            release_claim(session, delivery)
        settled = settle_delivery(session, settings, delivery, endpoint, event_body, client)
        stats[settled.status if settled.status in stats else "retrying"] += 1
    return stats


def run_worker_forever(session_factory, settings: Settings, poll_seconds: float = 2.0) -> None:
    """Long-running worker loop (used by `python -m app.worker`).

    Claims each batch under a fresh token, so running several of these against
    one database is safe: no two workers hold the same delivery at once.
    """
    import time as _time

    rate_limiter = RateLimiter(settings.redis_url)
    queue = DeliveryQueue(settings.redis_url)
    while True:
        session = session_factory()
        try:
            process_due_deliveries(
                session,
                settings,
                rate_limiter,
                claim_token=uuid.uuid4().hex,
            )
        finally:
            session.close()
        # Redis only shortens the wait: drain any wake-ups, then sleep.
        while True:
            queued_id = queue.dequeue(timeout=0)
            if not queued_id:
                break
        _time.sleep(poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    from .config import get_settings
    from .db import make_engine, make_session_factory

    _settings = get_settings()
    _engine = make_engine(_settings.database_url)
    run_worker_forever(make_session_factory(_engine), _settings)
