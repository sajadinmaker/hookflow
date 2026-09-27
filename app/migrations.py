"""Programmatic Alembic access.

Schema changes are a deployment step (`alembic upgrade head`), never something
the application does implicitly on boot. Booting and mutating schema from the
same process is how two replicas end up racing DDL against each other, and it
hides the fact that a migration was never written.

This module exists so tests and one-shot tooling can drive Alembic in-process
without shelling out.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade_to_head(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")


def downgrade_to_base(database_url: str | None = None) -> None:
    command.downgrade(alembic_config(database_url), "base")


def check_for_drift(database_url: str | None = None) -> bool:
    """True when the models and the migrations agree.

    Used as a CI gate so a model change without a matching migration fails the
    build instead of silently waiting for the next deploy.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from .config import get_settings
    from .db import Base

    url = database_url or get_settings().database_url
    engine = create_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()
    return not diff
