"""Shared pytest fixtures.

By default each test gets its own migrated SQLite file, so tests are fully
parallel-safe and cheap to run locally.

Set HOOKFLOW_TEST_DATABASE_URL to run the same suite against a real engine
(used by CI against PostgreSQL). In that mode migrations run once and the
tables are truncated between tests, because SQLite's per-test-file isolation has
no PostgreSQL equivalent. Running both is deliberate: dialect differences —
VARCHAR length enforcement, constraint enforcement, locking, timezone handling —
are precisely what SQLite cannot reveal.

The `client` fixture is pre-authenticated as a tenant, so tests that are not
about authorisation read naturally. `other_client` is a second tenant, used to
prove isolation.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXTERNAL_DATABASE_URL = os.environ.get("HOOKFLOW_TEST_DATABASE_URL", "").strip()
ADMIN_TOKEN = "test-admin-token"

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)


def _truncate(session) -> None:
    from sqlalchemy import text

    for table in ("deliveries", "events", "endpoints", "api_keys", "tenants"):
        session.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))
    session.commit()


@pytest.fixture()
def prepared_db():
    """Migrated, empty database plus its session factory."""
    from app.db import make_engine, make_session_factory
    from app.migrations import upgrade_to_head

    if EXTERNAL_DATABASE_URL:
        url = EXTERNAL_DATABASE_URL
        engine = make_engine(url)
        upgrade_to_head(url)
        factory = make_session_factory(engine)
        session = factory()
        _truncate(session)
        session.close()
    else:
        db_file = Path(tempfile.mkdtemp()) / "test.db"
        url = f"sqlite:///{db_file}"
        upgrade_to_head(url)
        engine = make_engine(url)
        factory = make_session_factory(engine)
    return url, factory


def _make_tenant(factory, name: str) -> tuple[str, str]:
    """Create a tenant and one API key. Returns (tenant_id, plaintext_key)."""
    from app.auth import generate_api_key
    from app.db import ApiKey, Tenant, new_id

    plaintext, prefix, key_hash = generate_api_key()
    session = factory()
    try:
        tenant = Tenant(id=new_id(), name=name)
        session.add(tenant)
        session.flush()
        session.add(
            ApiKey(
                id=new_id(),
                tenant_id=tenant.id,
                key_prefix=prefix,
                key_hash=key_hash,
                label="test",
            )
        )
        session.commit()
        return tenant.id, plaintext
    finally:
        session.close()


@pytest.fixture()
def tenant_keys(prepared_db):
    _url, factory = prepared_db
    a_id, a_key = _make_tenant(factory, "tenant-a")
    b_id, b_key = _make_tenant(factory, "tenant-b")
    return {"a": (a_id, a_key), "b": (b_id, b_key)}


def _make_app(prepared_db):
    """Build the ASGI app, pinning settings for the duration of the test."""
    from app import main as main_module
    from app.config import Settings
    from app.crypto import reset_secret_box_cache

    database_url, _factory = prepared_db
    os.environ["DATABASE_URL"] = database_url
    os.environ["REDIS_URL"] = ""
    reset_secret_box_cache()
    main_module.init_state(
        Settings(database_url=database_url, redis_url="", admin_token=ADMIN_TOKEN)
    )
    return main_module.create_app(
        Settings(database_url=database_url, redis_url="", admin_token=ADMIN_TOKEN)
    )


def _client_scope(app, api_key: str):
    """Yield a TestClient, then unpin the settings override.

    Without this teardown one test's pinned Settings would leak into the next,
    and the app would keep using a database it no longer owns.
    """
    from app.config import clear_settings_override

    try:
        with TestClient(app, headers={"Authorization": f"Bearer {api_key}"}) as c:
            yield c
    finally:
        clear_settings_override()


@pytest.fixture()
def client(prepared_db, tenant_keys):
    yield from _client_scope(_make_app(prepared_db), tenant_keys["a"][1])


@pytest.fixture()
def other_client(prepared_db, tenant_keys):
    """A second tenant. Must never see tenant-a's resources."""
    yield from _client_scope(_make_app(prepared_db), tenant_keys["b"][1])


@pytest.fixture()
def anon_client(prepared_db):
    """No credentials at all."""
    yield from _client_scope(_make_app(prepared_db), "hf_00000000_deadbeef")


@pytest.fixture()
def tenant_a(tenant_keys):
    return tenant_keys["a"][0]


@pytest.fixture()
def tenant_b(tenant_keys):
    return tenant_keys["b"][0]


@pytest.fixture()
def session(client):
    """A DB session bound to the same migrated test database as the client."""
    from app.main import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
