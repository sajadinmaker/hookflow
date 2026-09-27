"""Worker: success, retry-then-DLQ, rate-limit skip."""

import tempfile
from pathlib import Path

import httpx

from app.config import Settings
from app.db import Delivery, Endpoint, Event, make_engine, make_session_factory
from app.migrations import upgrade_to_head
from app.ratelimit import RateLimiter
from app.worker import process_due_deliveries


def _session():
    # A file-backed SQLite DB, not ":memory:": Alembic opens its own engine, and
    # an in-memory database would be empty again by the time the test connects.
    db_file = Path(tempfile.mkdtemp()) / "worker.db"
    url = f"sqlite:///{db_file}"
    engine = make_engine(url)
    upgrade_to_head(url)
    return make_session_factory(engine)()


def _seed(session, url="https://example.com/wh"):
    from app.db import Tenant, new_id

    tenant = Tenant(id=new_id(), name="worker-fixture")
    session.add(tenant)
    session.flush()
    ep = Endpoint(
        tenant_id=tenant.id, url=url, secret="s", rate_limit_per_minute=60
    )
    session.add(ep)
    session.flush()
    ev = Event(endpoint_id=ep.id, payload='{"a":1}')
    session.add(ev)
    session.flush()
    d = Delivery(event_id=ev.id, endpoint_id=ep.id, status="queued", attempts=0)
    session.add(d)
    session.commit()
    return ep, ev, d


def _ok_client():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _fail_client(status=500):
    def handler(request):
        return httpx.Response(status, json={"err": True})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_marks_delivered():
    session = _session()
    _, _, d = _seed(session)
    settings = Settings(database_url="sqlite://", redis_url="")
    stats = process_due_deliveries(session, settings, RateLimiter(), _ok_client())
    assert stats["success"] == 1
    assert session.get(Delivery, d.id).status == "success"


def test_failures_retry_then_dlq():
    session = _session()
    _, _, d = _seed(session)
    settings = Settings(database_url="sqlite://", redis_url="", max_attempts=2)
    stats = process_due_deliveries(session, settings, RateLimiter(), _fail_client())
    assert stats["retrying"] == 1
    row = session.get(Delivery, d.id)
    assert row.status == "retrying" and row.attempts == 1
    # Force due again and exhaust.
    row.next_attempt_at = None
    session.add(row)
    session.commit()
    stats = process_due_deliveries(session, settings, RateLimiter(), _fail_client())
    assert stats["dlq"] == 1
    assert session.get(Delivery, d.id).status == "dlq"


def test_rate_limited_skipped():
    session = _session()
    _, _, d = _seed(session)
    settings = Settings(database_url="sqlite://", redis_url="")
    limiter = RateLimiter()
    # Constrain this endpoint to 1/min and pre-consume its budget.
    delivery = session.get(Delivery, d.id)
    endpoint = session.get(Endpoint, delivery.endpoint_id)
    endpoint.rate_limit_per_minute = 1
    session.add(endpoint)
    session.commit()
    assert limiter.allow(endpoint.id, 1) is True
    stats = process_due_deliveries(session, settings, limiter, _ok_client())
    assert stats["rate_limited"] == 1
    assert session.get(Delivery, d.id).status == "queued"
