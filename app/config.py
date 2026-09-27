"""HookFlow configuration — environment-driven, no secrets in code."""

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
