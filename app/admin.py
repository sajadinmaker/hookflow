"""Admin surface: creating tenants and their API keys.

Guarded by `HOOKFLOW_ADMIN_TOKEN` via `X-Admin-Token`. If that setting is
empty the admin routes are disabled and return 503 — fail closed, because an
unauthenticated route that can mint credentials is not a bootstrap mechanism,
it is a vulnerability.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import generate_api_key
from .config import get_settings
from .db import ApiKey, Tenant, new_id, utcnow

ADMIN_TOKEN_HEADER = "X-Admin-Token"


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ApiKeyCreate(BaseModel):
    label: str = Field(default="", max_length=200)


class TenantOut(BaseModel):
    id: str
    name: str
    created_at: str


class ApiKeyOut(BaseModel):
    id: str
    tenant_id: str
    label: str
    key_prefix: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class ApiKeyCreated(BaseModel):
    """Returned once, carrying the only copy of the plaintext key."""

    id: str
    tenant_id: str
    label: str
    key_prefix: str
    created_at: str
    api_key: str


def require_admin(
    x_admin_token: str | None = Header(default=None, alias=ADMIN_TOKEN_HEADER),
) -> None:
    expected = get_settings().admin_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "admin API disabled: set HOOKFLOW_ADMIN_TOKEN to create tenants "
                "and API keys"
            ),
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token"
        )


def build_admin_router(get_db) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_admin)])

    @router.post("/tenants", status_code=201, response_model=TenantOut)
    def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)):
        if db.query(Tenant).filter(Tenant.name == payload.name).one_or_none():
            raise HTTPException(status_code=409, detail="tenant name already exists")
        tenant = Tenant(id=new_id(), name=payload.name)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return TenantOut(
            id=tenant.id, name=tenant.name, created_at=tenant.created_at.isoformat()
        )

    @router.get("/tenants", response_model=list[TenantOut])
    def list_tenants(db: Session = Depends(get_db)):
        rows = db.query(Tenant).order_by(Tenant.created_at).all()
        return [
            TenantOut(id=t.id, name=t.name, created_at=t.created_at.isoformat())
            for t in rows
        ]

    @router.post("/tenants/{tenant_id}/keys", status_code=201, response_model=ApiKeyCreated)
    def create_key(
        tenant_id: str, payload: ApiKeyCreate, db: Session = Depends(get_db)
    ):
        tenant = db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        plaintext, prefix, key_hash = generate_api_key()
        row = ApiKey(
            id=new_id(),
            tenant_id=tenant.id,
            key_prefix=prefix,
            key_hash=key_hash,
            label=payload.label,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return ApiKeyCreated(
            id=row.id,
            tenant_id=row.tenant_id,
            label=row.label,
            key_prefix=row.key_prefix,
            created_at=row.created_at.isoformat(),
            api_key=plaintext,
        )

    @router.get("/tenants/{tenant_id}/keys", response_model=list[ApiKeyOut])
    def list_keys(tenant_id: str, db: Session = Depends(get_db)):
        if db.get(Tenant, tenant_id) is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        rows = (
            db.query(ApiKey)
            .filter(ApiKey.tenant_id == tenant_id)
            .order_by(ApiKey.created_at)
            .all()
        )
        return [
            ApiKeyOut(
                id=k.id,
                tenant_id=k.tenant_id,
                label=k.label,
                key_prefix=k.key_prefix,
                created_at=k.created_at.isoformat(),
                last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
                revoked_at=k.revoked_at.isoformat() if k.revoked_at else None,
            )
            for k in rows
        ]

    @router.post("/keys/{key_id}/revoke", response_model=ApiKeyOut)
    def revoke_key(key_id: str, db: Session = Depends(get_db)):
        row = db.get(ApiKey, key_id)
        if row is None:
            raise HTTPException(status_code=404, detail="api key not found")
        row.revoked_at = utcnow()
        db.add(row)
        db.commit()
        db.refresh(row)
        return ApiKeyOut(
            id=row.id,
            tenant_id=row.tenant_id,
            label=row.label,
            key_prefix=row.key_prefix,
            created_at=row.created_at.isoformat(),
            last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
            revoked_at=row.revoked_at.isoformat(),
        )

    return router
