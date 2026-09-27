"""API-key authentication and tenant resolution.

Machine callers present `Authorization: Bearer hf_<prefix>_<secret>`. The
prefix is stored in the clear purely so a lookup touches one row; the secret is
stored only as a SHA-256 digest and compared in constant time.

Why a fast hash is correct here: the key is 256 bits of `secrets.token_hex`
output. There is no low-entropy secret to brute-force, so bcrypt/argon2 would
add latency on every request without adding resistance to anything. That
trade-off would flip immediately if keys were ever user-chosen.

Every authorised request resolves to exactly one tenant, and the tenant is
injected into each query. There is deliberately no way to read across tenants:
handlers receive a `Principal` and must filter by `principal.tenant_id`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

KEY_NAMESPACE = "hf"
PREFIX_LENGTH = 8  # hex characters of the key kept in the clear for lookup
SECRET_BYTES = 32

AUTH_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "


class AuthError(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext_key, key_prefix, key_hash).

    The plaintext is shown once and cannot be recovered afterwards.
    """
    prefix = secrets.token_hex(PREFIX_LENGTH // 2)
    secret = secrets.token_hex(SECRET_BYTES)
    plaintext = f"{KEY_NAMESPACE}_{prefix}_{secret}"
    return plaintext, prefix, hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def split_key(plaintext: str) -> tuple[str, str] | None:
    """Return (prefix, secret) if the key is well-formed, else None."""
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_NAMESPACE:
        return None
    prefix, secret = parts[1], parts[2]
    if not prefix or not secret:
        return None
    return prefix, secret


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Handlers must scope queries by tenant_id."""

    tenant_id: str
    tenant_name: str
    api_key_id: str

    def require_tenant(self, resource_tenant_id: str) -> None:
        """Raise unless the resource belongs to this principal's tenant.

        Defence in depth: a handler that forgets to filter should still fail
        closed rather than leak another tenant's data.
        """
        if resource_tenant_id != self.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith(BEARER_PREFIX):
        raise AuthError("missing or malformed Authorization header")
    token = authorization[len(BEARER_PREFIX) :].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def authenticate(db: Session, authorization: str | None) -> Principal:
    from .db import ApiKey, Tenant, utcnow

    token = _extract_bearer(authorization)
    parsed = split_key(token)
    if parsed is None:
        raise AuthError("malformed API key")
    prefix, _secret = parsed

    row = (
        db.query(ApiKey)
        .filter(ApiKey.key_prefix == prefix)
        .one_or_none()
    )
    if row is None:
        # Same error for unknown prefix and bad secret, so the response does
        # not reveal which prefixes exist.
        raise AuthError("invalid API key")
    if row.revoked_at is not None:
        raise AuthError("API key revoked")

    expected = row.key_hash
    if not hmac.compare_digest(expected, hash_api_key(token)):
        raise AuthError("invalid API key")

    tenant = db.get(Tenant, row.tenant_id)
    if tenant is None:  # pragma: no cover - FK prevents this
        raise AuthError("tenant no longer exists")

    row.last_used_at = utcnow()
    db.add(row)
    db.commit()
    return Principal(
        tenant_id=tenant.id, tenant_name=tenant.name, api_key_id=row.id
    )


def build_principal_dependency(get_db):
    """Build the FastAPI dependency that authenticates a bearer token.

    A factory rather than a module-level constant because the dependency needs
    this app's `get_db`, and importing app.main from here would be a cycle.
    """

    def require_principal(
        authorization: str | None = Header(default=None, alias=AUTH_HEADER),
        db: Session = Depends(get_db),
    ) -> Principal:
        return authenticate(db, authorization)

    return require_principal
