"""Correctness under the failure modes the code claims to handle.

The application has explicit recovery paths (idempotency race recovery, DLQ
transitions, rate-limit skips) that are easy to write and easy to leave untested,
which is exactly how a race window survives a refactor.
"""

import threading
from sqlalchemy import text

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.ratelimit import RateLimiter
from app.security import sign_payload, verify_signature


# --- idempotency: the database is the source of truth ----------------------


def test_duplicate_key_rejected_by_constraint(client):
    """Two rows with the same (endpoint, key) must be impossible at DB level.

    Application-level "check then insert" cannot close this window on its own;
    only the constraint can.
    """
    from app.db import Endpoint, Event
    from app.main import SessionLocal

    ep = client.post("/v1/endpoints", json={"url": "https://example.com/wh"}).json()

    session = SessionLocal()
    try:
        endpoint = Endpoint(url="https://example.com/wh", secret="x")
        session.add(endpoint)
        session.commit()

        session.add(Event(endpoint_id=endpoint.id, idempotency_key="k1", payload="{}"))
        session.commit()
        session.add(Event(endpoint_id=endpoint.id, idempotency_key="k1", payload="{}"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_null_idempotency_keys_are_not_deduplicated(client):
    """Ingest without a key must never collapse two distinct events into one."""
    ep = client.post("/v1/endpoints", json={"url": "https://example.com/wh"}).json()
    a = client.post("/v1/events", json={"endpoint_id": ep["id"], "payload": {"n": 1}}).json()
    b = client.post("/v1/events", json={"endpoint_id": ep["id"], "payload": {"n": 1}}).json()
    assert a["id"] != b["id"]
    assert not a["deduplicated"] and not b["deduplicated"]


def test_same_key_different_endpoints_are_independent(client):
    """Idempotency is scoped per endpoint, not global."""
    e1 = client.post("/v1/endpoints", json={"url": "https://a.example.com/wh"}).json()
    e2 = client.post("/v1/endpoints", json={"url": "https://b.example.com/wh"}).json()
    r1 = client.post(
        "/v1/events",
        json={"endpoint_id": e1["id"], "payload": {"n": 1}},
        headers={"Idempotency-Key": "shared"},
    ).json()
    r2 = client.post(
        "/v1/events",
        json={"endpoint_id": e2["id"], "payload": {"n": 1}},
        headers={"Idempotency-Key": "shared"},
    ).json()
    assert r1["id"] != r2["id"]
    assert not r1["deduplicated"] and not r2["deduplicated"]


def test_concurrent_ingest_with_same_key_creates_one_event(client):
    """The race the IntegrityError branch exists for.

    Each thread gets its own DB session, as real replicas would. Exactly one
    event may exist for the key, and every caller must be handed that same id
    rather than one of them getting a 500.
    """
    from app.db import Event
    from app.main import SessionLocal

    ep = client.post("/v1/endpoints", json={"url": "https://example.com/wh"}).json()
    url = f"{ep['id']}"
    results: list[tuple[int, str, bool]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)
    lock = threading.Lock()

    def ingest() -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            r = client.post(
                "/v1/events",
                json={"endpoint_id": url, "payload": {"n": 1}},
                headers={"Idempotency-Key": "race-key"},
            )
            with lock:
                results.append((r.status_code, r.json()["id"], r.json()["deduplicated"]))
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=ingest) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"concurrent ingest raised: {errors[0]!r}"
    assert len(results) == 4
    assert all(code == 201 for code, _, _ in results), f"a caller got an error: {results}"

    session = SessionLocal()
    try:
        rows = session.query(Event).filter(Event.idempotency_key == "race-key").all()
    finally:
        session.close()

    assert len(rows) == 1, f"expected exactly one stored event, got {len(rows)}"
    stored_id = rows[0].id
    assert {r[1] for r in results} == {stored_id}, "callers disagreed on the event id"
    assert sum(1 for r in results if not r[2]) == 1, "more than one caller thought it won"


# --- signature verification -------------------------------------------------


def test_signature_verifies_only_for_the_right_secret_and_body():
    body = b'{"a":1}'
    sig = sign_payload("s1", body)
    assert verify_signature("s1", body, sig) is True
    assert verify_signature("s2", body, sig) is False
    assert verify_signature("s1", b'{"a":2}', sig) is False


def test_signature_verification_is_timing_safe():
    """compare_digest must be used, not ==, or verification leaks the secret."""
    import inspect

    from app import security

    source = inspect.getsource(security.verify_signature)
    assert "compare_digest" in source
    assert "==" not in source.split("return")[-1]


# --- payload validation ----------------------------------------------------


def test_oversized_payload_rejected(client):
    from app.config import Settings as S

    limit = S().max_body_bytes
    ep = client.post("/v1/endpoints", json={"url": "https://example.com/wh"}).json()
    r = client.post(
        "/v1/events", json={"endpoint_id": ep["id"], "payload": {"blob": "x" * (limit + 10)}}
    )
    assert r.status_code == 413


def test_event_for_unknown_endpoint_is_404(client):
    r = client.post("/v1/events", json={"endpoint_id": "nope", "payload": {}})
    assert r.status_code == 404


# --- worker failure modes --------------------------------------------------


def test_unreachable_endpoint_retries_then_dlqs(client):
    """A connection error is retryable, and must not be mistaken for success."""
    import tempfile
    from pathlib import Path

    from app.db import Delivery, Endpoint, Event, make_engine, make_session_factory
    from app.migrations import upgrade_to_head
    from app.worker import process_due_deliveries

    url = f"sqlite:///{Path(tempfile.mkdtemp())/'w.db'}"
    engine = make_engine(url)
    upgrade_to_head(url)
    session = make_session_factory(engine)()

    ep = Endpoint(url="https://example.invalid/wh", secret="s", rate_limit_per_minute=60)
    session.add(ep)
    session.flush()
    ev = Event(endpoint_id=ep.id, payload='{"a":1}')
    session.add(ev)
    session.flush()
    d = Delivery(event_id=ev.id, endpoint_id=ep.id, status="queued", attempts=0)
    session.add(d)
    session.commit()

    def boom(request):
        raise httpx.ConnectError("name resolution failed")

    settings = Settings(database_url=url, redis_url="", max_attempts=2)
    stats = process_due_deliveries(
        session, settings, RateLimiter(), httpx.Client(transport=httpx.MockTransport(boom))
    )
    assert stats["retrying"] == 1
    row = session.get(Delivery, d.id)
    assert row.status == "retrying"
    assert row.last_status_code is None, "a transport error is not an HTTP status"
    assert "ConnectError" in (row.last_error or "")

    row.next_attempt_at = None
    session.add(row)
    session.commit()
    stats = process_due_deliveries(
        session, settings, RateLimiter(), httpx.Client(transport=httpx.MockTransport(boom))
    )
    assert stats["dlq"] == 1
    assert session.get(Delivery, d.id).status == "dlq"


def _set_fk_enforcement(session, enabled: bool) -> None:
    """Toggle FK enforcement per dialect.

    SQLite ignores foreign keys unless the pragma is set on each connection;
    PostgreSQL always enforces them and needs triggers disabled to simulate a
    row that bypasses referential integrity.
    """
    from sqlalchemy import text

    dialect = session.bind.dialect.name
    if dialect == "sqlite":
        session.execute(text(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}"))
    else:
        # Interpolated values are literals defined in this module, not input.
        role = "origin" if enabled else "replica"
        session.execute(text(f"SET session_replication_role = '{role}'"))
    session.commit()


def test_deleting_an_endpoint_removes_its_deliveries(client):
    """Endpoint.deliveries cascades at the database level.

    Worth pinning because SQLite ignores foreign keys unless the pragma is set,
    so without app/db.py enabling PRAGMA foreign_keys the local database would
    happily keep orphans that PostgreSQL would cascade away.
    """
    from app.db import Delivery
    from app.main import SessionLocal

    ep = client.post("/v1/endpoints", json={"url": "https://example.com/wh"}).json()
    r = client.post("/v1/events", json={"endpoint_id": ep["id"], "payload": {"n": 1}})
    delivery_id = r.json()["delivery_id"]

    session = SessionLocal()
    try:
        _set_fk_enforcement(session, True)
        session.execute(
            text("DELETE FROM endpoints WHERE id = :i"), {"i": ep["id"]}
        )
        session.commit()
        assert session.get(Delivery, delivery_id) is None
    finally:
        session.close()


def test_worker_dead_letters_a_delivery_whose_endpoint_vanished(client):
    """The worker's defensive branch, exercised by genuinely orphaning a row.

    Deleting an Endpoint through the ORM cascades its deliveries away, so this
    state can only arise from outside the ORM (raw SQL, a restore, or a schema
    where the cascade differs). The worker must not spin on it.
    """
    from app.db import Delivery
    from app.main import SessionLocal
    from app.worker import process_due_deliveries

    ep = client.post("/v1/endpoints", json={"url": "https://example.com/wh"}).json()
    r = client.post("/v1/events", json={"endpoint_id": ep["id"], "payload": {"n": 1}})
    delivery_id = r.json()["delivery_id"]

    session = SessionLocal()
    try:
        # Orphan the delivery without triggering the ORM relationship cascade.
        _set_fk_enforcement(session, False)
        session.execute(
            text("DELETE FROM endpoints WHERE id = :i"), {"i": ep["id"]}
        )
        session.commit()
        _set_fk_enforcement(session, True)

        stats = process_due_deliveries(
            session,
            Settings(database_url="sqlite://", redis_url=""),
            RateLimiter(),
            httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        )
        row = session.get(Delivery, delivery_id)
    finally:
        session.close()

    assert stats["dlq"] == 1
    assert row.status == "dlq"
    assert row.last_error == "endpoint deleted"


def test_backoff_schedule_is_monotonic_and_saturates():
    from app.worker import backoff_delay_seconds

    schedule = [60, 300, 1800, 7200, 43200]
    delays = [backoff_delay_seconds(i, schedule) for i in range(1, 8)]
    assert delays[: len(schedule)] == schedule, "first attempts must follow the schedule"
    assert all(d == schedule[-1] for d in delays[len(schedule) :]), (
        "past the schedule the delay must saturate, not keep growing"
    )
    assert backoff_delay_seconds(0, schedule) == schedule[0]
