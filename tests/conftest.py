"""Shared pytest fixtures.

By default each test gets its own migrated SQLite file, so tests are fully
parallel-safe and cheap to run locally.

Set HOOKFLOW_TEST_DATABASE_URL to run the same suite against a real engine
(used by CI against PostgreSQL). In that mode migrations run once and the
tables are truncated between tests, because SQLite's per-test-file isolation has
no PostgreSQL equivalent. Running both is deliberate: dialect differences —
constraint enforcement, locking, timezone handling — are precisely what SQLite
cannot reveal.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXTERNAL_DATABASE_URL = os.environ.get("HOOKFLOW_TEST_DATABASE_URL", "").strip()

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "")


def _external_session():
    from app.db import make_engine, make_session_factory
    from app.migrations import upgrade_to_head

    upgrade_to_head(EXTERNAL_DATABASE_URL)
    return make_session_factory(make_engine(EXTERNAL_DATABASE_URL))()


def _truncate(session) -> None:
    """Empty every table between tests when sharing one database."""
    from sqlalchemy import text

    session.execute(
        text(
            "TRUNCATE TABLE deliveries, events, endpoints "
            "RESTART IDENTITY CASCADE"
        )
    )
    session.commit()


@pytest.fixture()
def client(tmp_path):
    from app import main as main_module
    from app.config import Settings, get_settings
    from app.crypto import reset_secret_box_cache
    from app.migrations import upgrade_to_head

    if EXTERNAL_DATABASE_URL:
        database_url = EXTERNAL_DATABASE_URL
        _truncate(_external_session())
    else:
        db_file = Path(tempfile.mkdtemp()) / "test.db"
        database_url = f"sqlite:///{db_file}"
        upgrade_to_head(database_url)

    os.environ["DATABASE_URL"] = database_url
    os.environ["REDIS_URL"] = ""

    get_settings.cache_clear()
    reset_secret_box_cache()
    settings = Settings(database_url=database_url, redis_url="")

    # Fresh engine per test module import state.
    main_module.init_state(settings)
    app = main_module.create_app(settings)
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    reset_secret_box_cache()


@pytest.fixture()
def session(client):
    """A DB session bound to the same migrated test database as the client."""
    from app.main import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
