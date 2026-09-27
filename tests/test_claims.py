"""Worker claim/lease safety.

Two workers polling the same due delivery must never both POST it. These tests
assert disjointness of claims under real concurrency, not just that a claim
column exists.
"""

import os
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db import (
    Delivery,
    Endpoint,
    Event,
    make_engine,
    make_session_factory,
    utcnow,
)
from app.migrations import upgrade_to_head
from app.ratelimit import RateLimiter
from app.worker import (
    claim_due_deliveries,
    fetch_due_deliveries,
    process_due_deliveries,
    release_expired_claims,
)

# Claiming takes a different code path per dialect: PostgreSQL uses
# FOR UPDATE SKIP LOCKED, SQLite serialises writers. When an external database
# is configured, run here too, so the SKIP LOCKED branch is actually covered.
EXTERNAL_DATABASE_URL = os.environ.get("HOOKFLOW_TEST_DATABASE_URL", "").strip()


@pytest.fixture()
def claimed_db():
    if EXTERNAL_DATABASE_URL:
        url = EXTERNAL_DATABASE_URL
        engine = make_engine(url)
        upgrade_to_head(url)
        factory = make_session_factory(engine)
        session = factory()
        from tests.test_correctness import _set_fk_enforcement

        from tests.conftest import _truncate

        _truncate(session)
        session.close()
    else:
        url = f"sqlite:///{Path(tempfile.mkdtemp())/'claims.db'}"
        engine = make_engine(url)
        upgrade_to_head(url)
        factory = make_session_factory(engine)
    return url, factory


def _seed(session, count: int = 3) -> list[str]:
    from app.db import Tenant, new_id

    tenant = Tenant(id=new_id(), name="claim-fixture")
    session.add(tenant)
    session.flush()
    ids = []
    for _ in range(count):
        ep = Endpoint(
            tenant_id=tenant.id,
            url="https://example.com/wh",
            secret="s",
            rate_limit_per_minute=10000,
        )
        session.add(ep)
        session.flush()
        ev = Event(endpoint_id=ep.id, payload='{"a":1}')
        session.add(ev)
        session.flush()
        d = Delivery(event_id=ev.id, endpoint_id=ep.id, status="queued", attempts=0)
        session.add(d)
        session.flush()
        ids.append(d.id)
    session.commit()
    return ids


def test_claim_marks_rows_and_returns_them(claimed_db):
    url, factory = claimed_db
    session = factory()
    ids = _seed(session)
    claimed = claim_due_deliveries(session, "token-a", limit=10)
    assert {d.id for d in claimed} == set(ids)
    assert all(d.claim_token == "token-a" and d.claimed_at for d in claimed)
    session.close()


def test_a_second_claimer_gets_nothing(claimed_db):
    url, factory = claimed_db
    session = factory()
    _seed(session)
    first = claim_due_deliveries(session, "token-a", limit=10)
    second = claim_due_deliveries(session, "token-b", limit=10)
    assert first and not second, "an already-claimed row must not be re-claimable"
    session.close()


def test_concurrent_claimers_receive_disjoint_rows(claimed_db):
    """The regression this whole mechanism exists for.

    Both workers select the same free rows at the same instant. Each row must
    end up in exactly one worker's batch.
    """
    url, factory = claimed_db
    session = factory()
    ids = _seed(session, count=12)
    session.close()

    results: dict[str, set[str]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def claim(token: str) -> None:
        own = factory()
        try:
            barrier.wait(timeout=10)
            got = {d.id for d in claim_due_deliveries(own, token, limit=12)}
            with lock:
                results[token] = got
        except BaseException as exc:  # noqa: BLE001 - asserted below
            with lock:
                errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=claim, args=(t,)) for t in ("token-a", "token-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"claiming raised: {errors[0]!r}"
    a, b = results["token-a"], results["token-b"]
    assert not (a & b), f"rows claimed by both workers: {sorted(a & b)}"
    assert a | b == set(ids), "every due row must be claimed by exactly one worker"


def _age_claim(session, delivery_id: str, seconds: int) -> None:
    """Backdate a claim.

    Assigns an absolute timestamp rather than subtracting from the value read
    back, because SQLite returns naive datetimes for DateTime(timezone=True)
    and a Python-side comparison against an aware utcnow() would raise. The
    lease predicate is evaluated in SQL, so this matches production behaviour.
    """
    session.query(Delivery).filter(Delivery.id == delivery_id).update(
        {Delivery.claimed_at: utcnow() - timedelta(seconds=seconds)},
        synchronize_session=False,
    )
    session.commit()


def test_expired_lease_becomes_claimable_again(claimed_db):
    """A worker that dies mid-delivery must not strand its row forever."""
    url, factory = claimed_db
    session = factory()
    ids = _seed(session)
    claim_due_deliveries(session, "dead-worker", limit=10, lease_seconds=60.0)
    _age_claim(session, ids[0], seconds=120)

    reclaimed = claim_due_deliveries(session, "fresh-worker", limit=10, lease_seconds=60.0)
    assert ids[0] in {d.id for d in reclaimed}
    assert session.get(Delivery, ids[0]).claim_token == "fresh-worker"
    session.close()


def test_release_expired_claims_frees_only_stale_rows(claimed_db):
    url, factory = claimed_db
    session = factory()
    ids = _seed(session, count=2)
    claim_due_deliveries(session, "dead", limit=10, lease_seconds=60.0)
    _age_claim(session, ids[0], seconds=300)

    assert release_expired_claims(session, lease_seconds=60.0) == 1
    assert session.get(Delivery, ids[0]).claim_token is None
    assert session.get(Delivery, ids[1]).claim_token == "dead", "live lease must be kept"
    session.close()


def test_successful_settle_releases_the_claim(claimed_db):
    """After delivery the row is finished, so nothing should still hold it."""
    import httpx

    url, factory = claimed_db
    session = factory()
    ids = _seed(session, count=2)
    settings = Settings(database_url=url, redis_url="", claim_lease_seconds=60.0)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    process_due_deliveries(session, settings, RateLimiter(), client, claim_token="tok")
    assert all(session.get(Delivery, i).claim_token is None for i in ids)
    assert all(session.get(Delivery, i).status == "success" for i in ids)
    session.close()


def test_rate_limited_delivery_is_not_stranded(claimed_db):
    """A skipped row must release its claim, or a full rate-limit window is a stall."""
    import httpx

    url, factory = claimed_db
    session = factory()
    ids = _seed(session, count=1)
    limiter = RateLimiter()
    # The limiter is keyed by endpoint, not delivery.
    endpoint_id = session.get(Delivery, ids[0]).endpoint_id
    session.get(Endpoint, endpoint_id).rate_limit_per_minute = 1
    session.commit()
    assert limiter.allow(endpoint_id, 1) is True  # exhaust the budget

    settings = Settings(database_url=url, redis_url="", claim_lease_seconds=60.0)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    stats = process_due_deliveries(
        session, settings, limiter, client, claim_token="tok"
    )
    assert stats["rate_limited"] == 1
    row = session.get(Delivery, ids[0])
    assert row.claim_token is None, "rate-limited row kept a claim and would stall"
    assert row.status == "queued"
    session.close()


def test_unclaimed_mode_still_selects_everything(claimed_db):
    """Single-worker path is unchanged: no claim bookkeeping required."""
    url, factory = claimed_db
    session = factory()
    ids = _seed(session)
    assert {d.id for d in fetch_due_deliveries(session, 10)} == set(ids)
    session.close()
