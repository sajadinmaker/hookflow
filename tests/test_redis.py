"""Redis-backed queue and rate limiting.

Skipped automatically when no Redis is reachable, so the suite still runs on a
laptop with nothing but SQLite. CI runs a Redis service so these execute there.
"""

import os
import uuid

import pytest

from app.queue import QUEUE_KEY, DeliveryQueue
from app.ratelimit import MemoryRateLimiter, RateLimiter

REDIS_URL = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"


def _redis_available() -> bool:
    try:
        import redis

        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


needs_redis = pytest.mark.skipif(
    not _redis_available(), reason="no Redis reachable; set REDIS_URL to enable"
)


# --- behaviour that must hold with or without Redis ------------------------


def test_memory_limiter_allows_up_to_the_limit():
    limiter = MemoryRateLimiter()
    assert [limiter.allow("k", 2) for _ in range(4)] == [True, True, False, False]


def test_memory_limiter_window_expires():
    limiter = MemoryRateLimiter()
    assert limiter.allow("k", 1, now=1000.0) is True
    assert limiter.allow("k", 1, now=1030.0) is False
    # Past the 60s window the earlier hit no longer counts.
    assert limiter.allow("k", 1, now=1061.0) is True


def test_queue_without_redis_is_a_safe_noop():
    """Without Redis the queue must silently do nothing, never raise."""
    queue = DeliveryQueue("")
    assert queue.has_redis is False
    queue.enqueue("x")  # must not raise
    assert queue.dequeue(timeout=1) is None


# --- Redis-backed ----------------------------------------------------------


@needs_redis
def test_redis_queue_is_fifo_and_drains():
    queue = DeliveryQueue(REDIS_URL)
    queue._redis.delete(QUEUE_KEY)
    try:
        for delivery_id in ("first", "second", "third"):
            queue.enqueue(delivery_id)
        assert [queue.dequeue(timeout=2) for _ in range(3)] == [
            "first",
            "second",
            "third",
        ]
        assert queue.dequeue(timeout=1) is None
    finally:
        queue._redis.delete(QUEUE_KEY)


@needs_redis
def test_redis_limiter_enforces_and_isolates_keys():
    limiter = RateLimiter(REDIS_URL)
    a, b = f"ep-{uuid.uuid4().hex}", f"ep-{uuid.uuid4().hex}"
    try:
        assert [limiter.allow(a, 3) for _ in range(5)] == [
            True,
            True,
            True,
            False,
            False,
        ]
        # A different endpoint must not consume the first one's budget.
        assert limiter.allow(b, 3) is True
    finally:
        for key in (a, b):
            for window in range(0, 3):
                limiter._redis.delete(f"hookflow:rl:{key}:{window}")


@needs_redis
def test_redis_limiter_is_scoped_to_the_current_minute_window():
    """The window index is derived from the clock and embedded in the key.

    That is what stops a counter leaking into the next minute; asserting on
    specific window numbers would be testing the clock, not the limiter.
    """
    import time

    limiter = RateLimiter(REDIS_URL)
    key = f"ep-{uuid.uuid4().hex}"
    window = int(time.time() // 60)
    redis_key = f"hookflow:rl:{key}:{window}"
    limiter._redis.delete(redis_key)
    try:
        assert limiter.allow(key, 1) is True
        assert limiter.allow(key, 1) is False
        assert limiter._redis.exists(redis_key) == 1, "counter must be stored under the windowed key"
    finally:
        limiter._redis.delete(redis_key)


@needs_redis
def test_limiter_sets_ttl_so_keys_do_not_leak_forever():
    limiter = RateLimiter(REDIS_URL)
    key = f"ep-{uuid.uuid4().hex}"
    try:
        limiter.allow(key, 5)
        redis_key = f"hookflow:rl:{key}:{int(__import__('time').time() // 60)}"
        ttl = limiter._redis.ttl(redis_key)
        assert 0 < ttl <= 65, f"expected a bounded TTL, got {ttl}"
    finally:
        limiter._redis.delete(f"hookflow:rl:{key}:{int(__import__('time').time() // 60)}")
