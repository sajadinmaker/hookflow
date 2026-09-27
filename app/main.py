"""HookFlow API: event ingestion, webhook registration, delivery history."""

from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .admin import build_admin_router
from .auth import Principal, build_principal_dependency
from .config import Settings, get_settings, set_settings
from .crypto import get_secret_box
from .db import Delivery, Endpoint, Event, make_engine, utcnow
from .queue import DeliveryQueue
from .ratelimit import RateLimiter
from .schemas import (
    DeliveryOut,
    EndpointCreate,
    EndpointCreated,
    EndpointOut,
    EventCreate,
    EventOut,
)
from .security import generate_secret

rate_limiter = RateLimiter()
delivery_queue = DeliveryQueue()
engine = None
SessionLocal = None

logger = logging.getLogger("hookflow")

# Lightweight in-process counters (also exposed as Prometheus text at /metrics).
# Keeps the dependency set small: no prometheus_client required.
_counters: dict[str, int] = {
    "events_ingested_total": 0,
    "events_deduplicated_total": 0,
    "deliveries_requeued_total": 0,
}


def _bump(counter: str) -> None:
    _counters[counter] = _counters.get(counter, 0) + 1


def get_settings_dep() -> Settings:
    return get_settings()


def init_state(settings: Settings | None = None) -> None:
    global engine, SessionLocal, rate_limiter, delivery_queue
    settings = settings or get_settings()
    # Pin these settings process-wide. Handlers and the admin routes resolve
    # configuration through get_settings(), so without this an explicitly
    # supplied Settings would steer only the engine and leave the rest of the
    # app reading the environment.
    set_settings(settings)
    # One engine factory for every environment, so SQLite dev/test and
    # PostgreSQL production cannot drift in behaviour.
    engine = make_engine(settings.database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # NOTE: schema is NOT created here. Run `alembic upgrade head` as a deploy
    # step. See app/migrations.py and docs/deployment.md.
    # Rebind helpers to current settings (tests override redis_url="").
    rate_limiter = RateLimiter(settings.redis_url)
    delivery_queue = DeliveryQueue(settings.redis_url)


def get_db():
    assert SessionLocal is not None, "call init_state() first"
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def to_delivery_out(d: Delivery) -> DeliveryOut:
    return DeliveryOut(
        id=d.id,
        event_id=d.event_id,
        endpoint_id=d.endpoint_id,
        status=d.status,
        attempts=d.attempts,
        next_attempt_at=d.next_attempt_at.isoformat() if d.next_attempt_at else None,
        last_status_code=d.last_status_code,
        last_error=d.last_error,
        latency_ms=d.latency_ms,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    init_state(settings or get_settings())
    app = FastAPI(title="HookFlow", version="0.2.0")
    require_principal = build_principal_dependency(get_db)
    app.include_router(build_admin_router(get_db))

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """Attach a request ID for log correlation. Honors incoming X-Request-ID."""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed", extra={"request_id": request_id})
            raise
        latency_ms = int((time.monotonic() - start) * 1000)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s -> %s (%sms)",
            request.method,
            request.url.path,
            getattr(response, "status_code", "?"),
            latency_ms,
            extra={"request_id": request_id},
        )
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "hookflow"}

    @app.get("/metrics")
    def metrics():
        """Prometheus-text metrics. No dependency; scrape-ready exposition format."""
        lines = [
            "# HELP hookflow_events_ingested_total Accepted events (excluding dedupes).",
            "# TYPE hookflow_events_ingested_total counter",
            f"hookflow_events_ingested_total {_counters.get('events_ingested_total', 0)}",
            "# HELP hookflow_events_deduplicated_total Idempotent replays returned without insert.",
            "# TYPE hookflow_events_deduplicated_total counter",
            f"hookflow_events_deduplicated_total {_counters.get('events_deduplicated_total', 0)}",
            "# HELP hookflow_deliveries_requeued_total DLQ/manual requeues via replay endpoint.",
            "# TYPE hookflow_deliveries_requeued_total counter",
            f"hookflow_deliveries_requeued_total {_counters.get('deliveries_requeued_total', 0)}",
        ]
        return Response(content="\n".join(lines) + "\n", media_type="text/plain")

    @app.get("/v1/stats")
    def stats(
        db: Session = Depends(get_db),
        principal: Principal = Depends(require_principal),
    ):
        """Queue-depth + delivery-state overview for dashboards/alerts."""
        rows = (
            db.query(Delivery.status, func.count(Delivery.id))
            .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
            .filter(Endpoint.tenant_id == principal.tenant_id)
            .group_by(Delivery.status)
            .all()
        )
        by_status = {status: count for status, count in rows}
        queued = by_status.get("queued", 0) + by_status.get("retrying", 0)
        return {
            "by_status": by_status,
            "queue_depth": queued,
            "redis": "configured" if delivery_queue.has_redis else "not-configured",
        }

    @app.get("/ready")
    def ready(db: Session = Depends(get_db)):
        checks: dict = {}
        try:
            db.query(Endpoint.id).limit(1).all()
            checks["database"] = "ok"
        except OperationalError as exc:
            # Almost always "relation does not exist", i.e. migrations were
            # never applied. Say so plainly rather than returning a bare 500.
            raise HTTPException(
                status_code=503,
                detail=(
                    "database schema is not present or unreachable; "
                    "run `alembic upgrade head` before starting the app"
                ),
            ) from exc
        # Redis check (optional dependency).
        if delivery_queue.has_redis:
            try:
                delivery_queue._redis.ping()  # type: ignore[union-attr]
                checks["redis"] = "ok"
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"redis unreachable: {exc}")
        else:
            checks["redis"] = "not-configured"
        # Secret encryption posture. A development key is a deploy mistake, not a
        # detail: the readiness probe is what an operator actually watches.
        box = get_secret_box(get_settings().secret_encryption_key)
        checks["secret_encryption"] = "development-key" if box.using_development_key else "ok"
        return {"status": "ok", **checks}

    @app.post("/v1/endpoints", status_code=201, response_model=EndpointCreated)
    def register_endpoint(
        payload: EndpointCreate,
        db: Session = Depends(get_db),
        principal: Principal = Depends(require_principal),
    ):
        settings = get_settings()
        body_len = len(payload.url.encode())
        if body_len > 2000:
            raise HTTPException(status_code=413, detail="url too long")
        # The secret is encrypted before it ever reaches the database and is
        # returned to the caller exactly once, in this response.
        plaintext_secret = payload.secret or generate_secret()
        endpoint = Endpoint(
            tenant_id=principal.tenant_id,
            url=payload.url,
            secret=get_secret_box(settings.secret_encryption_key).encrypt(plaintext_secret),
            rate_limit_per_minute=payload.rate_limit_per_minute,
        )
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)
        return EndpointCreated(
            id=endpoint.id,
            url=endpoint.url,
            rate_limit_per_minute=endpoint.rate_limit_per_minute,
            created_at=endpoint.created_at.isoformat(),
            secret=plaintext_secret,
        )

    @app.get("/v1/endpoints")
    def list_endpoints(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        principal: Principal = Depends(require_principal),
    ):
        rows = (
            db.query(Endpoint)
            .filter(Endpoint.tenant_id == principal.tenant_id)
            .order_by(Endpoint.created_at)
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "items": [
                {
                    "id": e.id,
                    "url": e.url,
                    "rate_limit_per_minute": e.rate_limit_per_minute,
                    "created_at": e.created_at.isoformat(),
                }
                for e in rows
            ]
        }

    @app.post("/v1/events", status_code=201, response_model=EventOut)
    def ingest_event(
        payload: EventCreate,
        db: Session = Depends(get_db),
        principal: Principal = Depends(require_principal),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        settings = get_settings()
        key = payload.idempotency_key or idempotency_key
        endpoint = db.get(Endpoint, payload.endpoint_id)
        # 404 rather than 403: another tenant's endpoint must be indistinguishable
        # from one that does not exist.
        if endpoint is None or endpoint.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="endpoint not found")
        try:
            raw = json.dumps(payload.payload, separators=(",", ":"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="payload must be JSON-serializable")
        if len(raw.encode()) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="payload too large")
        if key:
            existing = (
                db.query(Event)
                .filter(Event.endpoint_id == endpoint.id, Event.idempotency_key == key)
                .order_by(Event.created_at)
                .first()
            )
            if existing is not None:
                _bump("events_deduplicated_total")
                delivery = (
                    db.query(Delivery)
                    .filter(Delivery.event_id == existing.id)
                    .order_by(Delivery.created_at)
                    .first()
                )
                return EventOut(
                    id=existing.id,
                    endpoint_id=endpoint.id,
                    delivery_id=delivery.id if delivery else "",
                    deduplicated=True,
                )
        event = Event(endpoint_id=endpoint.id, idempotency_key=key, payload=raw)
        db.add(event)
        try:
            db.flush()
        except IntegrityError:
            # Lost the race: a concurrent ingest with the same
            # (endpoint_id, idempotency_key) won. Return the winner instead
            # of 500ing. This is why the UNIQUE constraint exists.
            db.rollback()
            winner = (
                db.query(Event)
                .filter(Event.endpoint_id == endpoint.id, Event.idempotency_key == key)
                .order_by(Event.created_at)
                .first()
            )
            if winner is None:  # pragma: no cover — defensive, should not happen
                raise HTTPException(status_code=409, detail="idempotency conflict")
            _bump("events_deduplicated_total")
            delivery = (
                db.query(Delivery)
                .filter(Delivery.event_id == winner.id)
                .order_by(Delivery.created_at)
                .first()
            )
            return EventOut(
                id=winner.id,
                endpoint_id=endpoint.id,
                delivery_id=delivery.id if delivery else "",
                deduplicated=True,
            )
        delivery = Delivery(
            event_id=event.id,
            endpoint_id=endpoint.id,
            status="queued",
            attempts=0,
            next_attempt_at=None,
        )
        db.add(delivery)
        db.commit()
        db.refresh(event)
        db.refresh(delivery)
        delivery_queue.enqueue(delivery.id)
        _bump("events_ingested_total")
        return EventOut(
            id=event.id,
            endpoint_id=endpoint.id,
            delivery_id=delivery.id,
            deduplicated=False,
        )

    @app.get("/v1/events/{event_id}")
    def get_event(
        event_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(require_principal),
    ):
        event = db.get(Event, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        principal.require_tenant(
            db.get(Endpoint, event.endpoint_id).tenant_id
        )
        deliveries = (
            db.query(Delivery).filter(Delivery.event_id == event.id).order_by(
                Delivery.created_at
            ).all()
        )
        return {
            "id": event.id,
            "endpoint_id": event.endpoint_id,
            "payload": json.loads(event.payload),
            "created_at": event.created_at.isoformat(),
            "deliveries": [to_delivery_out(d).model_dump() for d in deliveries],
        }

    @app.get("/v1/deliveries")
    def list_deliveries(
        status: str | None = Query(default=None),
        endpoint_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        principal: Principal = Depends(require_principal),
    ):
        # Scope to the caller's endpoints via a join; a delivery id from another
        # tenant is therefore unreachable, not merely filtered from the listing.
        q = db.query(Delivery).join(
            Endpoint, Delivery.endpoint_id == Endpoint.id
        ).filter(Endpoint.tenant_id == principal.tenant_id)
        if status:
            if status not in ("queued", "success", "retrying", "dlq"):
                raise HTTPException(status_code=422, detail="invalid status")
            q = q.filter(Delivery.status == status)
        if endpoint_id:
            q = q.filter(Delivery.endpoint_id == endpoint_id)
        rows = q.order_by(Delivery.created_at.desc()).offset(offset).limit(limit).all()
        return {"items": [to_delivery_out(d).model_dump() for d in rows]}

    @app.post("/v1/deliveries/{delivery_id}/requeue", response_model=DeliveryOut)
    def requeue_delivery(
        delivery_id: str,
        db: Session = Depends(get_db),
        principal: Principal = Depends(require_principal),
    ):
        """Replay a dead-letter (or retrying/queued) delivery.

        Resets attempts so the worker treats it as fresh. Only DLQ +
        retrying + queued are requeueable — replaying a success would
        duplicate a side effect at the receiver.
        """
        delivery = db.get(Delivery, delivery_id)
        if delivery is None:
            raise HTTPException(status_code=404, detail="delivery not found")
        endpoint = db.get(Endpoint, delivery.endpoint_id)
        if endpoint is None or endpoint.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail="delivery not found")
        if delivery.status not in ("dlq", "retrying", "queued"):
            raise HTTPException(
                status_code=409,
                detail=f"cannot requeue delivery in status {delivery.status!r}",
            )
        delivery.status = "queued"
        delivery.attempts = 0
        delivery.next_attempt_at = None
        delivery.last_error = None
        delivery.updated_at = utcnow()
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        delivery_queue.enqueue(delivery.id)
        _bump("deliveries_requeued_total")
        return to_delivery_out(delivery)

    return app


app = create_app()
