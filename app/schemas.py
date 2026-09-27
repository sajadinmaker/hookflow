"""Pydantic request/response schemas (Pydantic v2)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class EndpointCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    secret: str | None = Field(default=None, max_length=128)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10000)

    @field_validator("url")
    @classmethod
    def must_be_http(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class EndpointOut(BaseModel):
    id: str
    url: str
    rate_limit_per_minute: int
    created_at: str


class EndpointCreated(EndpointOut):
    """Creation response only.

    Carries the plaintext secret exactly once, because the receiver needs it to
    verify HMAC signatures. It is never returned again — at rest the value is
    encrypted, and there is deliberately no endpoint that will hand it back.
    """

    secret: str


class EventCreate(BaseModel):
    endpoint_id: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]
    idempotency_key: str | None = Field(default=None, max_length=128)


class EventOut(BaseModel):
    id: str
    endpoint_id: str
    delivery_id: str
    deduplicated: bool = False


class DeliveryOut(BaseModel):
    id: str
    event_id: str
    endpoint_id: str
    status: str
    attempts: int
    next_attempt_at: str | None
    last_status_code: int | None
    last_error: str | None
    latency_ms: int | None
