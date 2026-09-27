"""HookFlow configuration — environment-driven, no secrets in code."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "hookflow"
    # SQLite by default for local dev/tests; set to postgresql+psycopg://... in production.
    database_url: str = "sqlite:///./hookflow.db"
    # Optional. When unset, rate limiting and the queue use in-process memory
    # (single replica only) — documented in docs/architecture.md.
    redis_url: str = ""
    max_body_bytes: int = 256 * 1024
    delivery_timeout_seconds: float = 10.0
    max_attempts: int = 5
    # Exponential backoff delays (seconds) per failed attempt index.
    backoff_schedule_seconds: list[int] = [60, 300, 1800, 7200, 43200]
    default_rate_limit_per_minute: int = 60
    # Symmetric key protecting webhook secrets at rest. Accepts a Fernet key or
    # any passphrase (SHA-256 stretched). Leave empty in local dev and a
    # well-known development key is used — /ready reports that state so it cannot
    # pass unnoticed in production.
    secret_encryption_key: str = ""
    # How long a worker's claim on a delivery stays valid. Must comfortably
    # exceed one delivery attempt (delivery_timeout_seconds) or a slow but
    # healthy worker will have its row stolen mid-flight, and must be short
    # enough that a crashed worker's rows are picked up quickly.
    claim_lease_seconds: float = 60.0
    # Bearer token for the admin surface (create tenants / API keys). Empty
    # disables those routes entirely with a 503 rather than leaving an
    # unauthenticated route that can mint credentials.
    admin_token: str = ""


@lru_cache
def _from_environment() -> Settings:
    return Settings()


_settings_override: Settings | None = None


def set_settings(settings: Settings) -> None:
    """Pin the settings the whole process should use.

    Without this, `create_app(settings)` only steered the database engine while
    handlers kept calling `get_settings()` and reading the environment. The two
    could disagree — most dangerously, storing secrets under one key while the
    engine pointed at another database. `init_state` calls this so an explicitly
    supplied Settings is authoritative for the process.
    """
    global _settings_override
    _settings_override = settings
    _from_environment.cache_clear()


def clear_settings_override() -> None:
    global _settings_override
    _settings_override = None
    _from_environment.cache_clear()


def get_settings() -> Settings:
    if _settings_override is not None:
        return _settings_override
    return _from_environment()
