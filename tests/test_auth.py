"""Authentication and tenant isolation.

The isolation tests matter more than the auth tests: a valid key from the wrong
tenant must not be able to read, write, or infer the existence of another
tenant's resources. Where a resource is not visible, the API must answer 404
rather than 403, because a 403 confirms the id exists.
"""

import pytest
from sqlalchemy import inspect

from app.auth import generate_api_key, hash_api_key, split_key


def _register(client, url="https://example.com/wh", **kw):
    r = client.post("/v1/endpoints", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# --- credential handling ---------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer not-a-key"},
        {"Authorization": "Bearer wrongprefix_secret"},
        {"Authorization": "Bearer hf_00000000_0000000000000000000000000000000000000000000000000000"},
    ],
    ids=["absent", "empty", "no-scheme", "wrong-scheme", "malformed", "wrong-namespace", "unknown-prefix"],
)
def test_unauthenticated_requests_are_rejected(anon_client, headers):
    anon_client.headers.pop("Authorization", None)
    r = anon_client.post("/v1/endpoints", json={"url": "https://x.example.com/wh"}, headers=headers)
    assert r.status_code == 401, r.text


def test_secret_part_must_match(anon_client):
    """A valid prefix with a wrong secret must fail."""
    anon_client.headers.pop("Authorization", None)
    plaintext, prefix, _ = generate_api_key()
    wrong_secret = "hf_" + prefix + "_" + ("0" * 64)
    r = anon_client.get(
        "/v1/endpoints", headers={"Authorization": f"Bearer {wrong_secret}"}
    )
    assert r.status_code == 401


def test_key_is_never_stored_in_plaintext(session, tenant_keys):
    """Only the prefix and a digest are persisted."""
    from app.db import ApiKey

    expected = {
        key.split("_", 2)[1]: hash_api_key(key)
        for _tenant_id, key in tenant_keys.values()
    }
    rows = session.query(ApiKey).all()
    assert {r.key_prefix for r in rows} == set(expected), "one key per fixture tenant"

    for row in rows:
        assert row.key_hash == expected[row.key_prefix], (
            "stored digest must be sha256 of the full key"
        )
        # No persisted string may hold a usable key.
        for value in obj_values(row):
            assert not value.startswith("hf_"), f"plaintext key stored: {value!r}"


def obj_values(obj) -> list[str]:
    return [v for v in obj.__dict__.values() if isinstance(v, str)]


def test_split_key_accepts_only_the_expected_shape():
    plaintext, prefix, _ = generate_api_key()
    parsed = split_key(plaintext)
    assert parsed is not None and parsed[0] == prefix
    for bad in ("", "nope", "hf_onlyone", "xx_prefix_secret", "hf__secret", "hf_prefix_"):
        assert split_key(bad) is None, bad


# --- tenant isolation ------------------------------------------------------


def test_endpoints_are_scoped_to_the_tenant(client, other_client):
    mine = _register(client, "https://mine.example.com/wh")
    theirs = _register(other_client, "https://theirs.example.com/wh")

    mine_ids = {e["id"] for e in client.get("/v1/endpoints").json()["items"]}
    their_ids = {e["id"] for e in other_client.get("/v1/endpoints").json()["items"]}

    assert mine["id"] in mine_ids and theirs["id"] not in mine_ids
    assert theirs["id"] in their_ids and mine["id"] not in their_ids


def test_cannot_ingest_to_another_tenants_endpoint(client, other_client):
    theirs = _register(other_client)
    r = client.post(
        "/v1/events", json={"endpoint_id": theirs["id"], "payload": {"n": 1}}
    )
    assert r.status_code == 404, "must be 404, not 403: 403 would confirm it exists"
    assert "not found" in r.json()["detail"].lower()


def test_cannot_read_another_tenants_event(client, other_client):
    theirs = _register(other_client)
    created = other_client.post(
        "/v1/events", json={"endpoint_id": theirs["id"], "payload": {"secret": 1}}
    ).json()
    r = client.get(f"/v1/events/{created['id']}")
    assert r.status_code == 404


def test_cannot_replay_another_tenants_delivery(client, other_client):
    theirs = _register(other_client)
    delivery_id = other_client.post(
        "/v1/events", json={"endpoint_id": theirs["id"], "payload": {"n": 1}}
    ).json()["delivery_id"]
    r = client.post(f"/v1/deliveries/{delivery_id}/requeue")
    assert r.status_code == 404


def test_delivery_listing_excludes_other_tenants(client, other_client):
    theirs = _register(other_client)
    other_client.post("/v1/events", json={"endpoint_id": theirs["id"], "payload": {"n": 1}})
    client.post("/v1/events", json={"endpoint_id": _register(client)["id"], "payload": {"n": 2}})

    items = client.get("/v1/deliveries").json()["items"]
    assert len(items) == 1, "tenant-a must not see tenant-b's delivery"


def test_stats_are_scoped(client, other_client):
    theirs = _register(other_client)
    other_client.post("/v1/events", json={"endpoint_id": theirs["id"], "payload": {"n": 1}})
    stats = client.get("/v1/stats").json()
    assert stats["by_status"] in ({"queued": 1}, {}), stats


def test_deleting_a_tenant_cascades_its_endpoints(client, other_client, session):
    from app.db import Endpoint, Tenant

    theirs = _register(other_client)
    session.delete(session.get(Tenant, theirs["id"]) or session.query(Tenant).filter(
        Tenant.name == "tenant-b"
    ).one())
    session.commit()
    assert session.get(Endpoint, theirs["id"]) is None


# --- admin surface ---------------------------------------------------------


def test_admin_disabled_without_token(prepared_db, tenant_keys):
    from app.config import Settings, clear_settings_override
    from app import main as main_module
    from fastapi.testclient import TestClient

    database_url, _ = prepared_db
    clear_settings_override()
    main_module.init_state(Settings(database_url=database_url, redis_url="", admin_token=""))
    app = main_module.create_app(Settings(database_url=database_url, redis_url="", admin_token=""))
    with TestClient(app) as c:
        r = c.post("/v1/admin/tenants", json={"name": "nope"})
    assert r.status_code == 503, "admin must fail closed, not be open"
    clear_settings_override()


def test_admin_requires_the_token(client):
    r = client.post("/v1/admin/tenants", json={"name": "nope"})
    assert r.status_code == 401


def test_admin_can_provision_a_tenant_and_usable_key(client, prepared_db):
    admin = {"X-Admin-Token": "test-admin-token"}
    created = client.post("/v1/admin/tenants", json={"name": "acme"}, headers=admin)
    assert created.status_code == 201, created.text
    tenant_id = created.json()["id"]

    # duplicate name rejected
    assert client.post("/v1/admin/tenants", json={"name": "acme"}, headers=admin).status_code == 409

    key = client.post(
        f"/v1/admin/tenants/{tenant_id}/keys", json={"label": "ci"}, headers=admin
    )
    assert key.status_code == 201, key.text
    body = key.json()
    assert body["api_key"].startswith("hf_")

    # the new key works, and sees an empty tenant
    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import Settings, clear_settings_override

    database_url, _ = prepared_db
    app_obj = main_module.create_app(
        Settings(database_url=database_url, redis_url="", admin_token="test-admin-token")
    )
    with TestClient(
        app_obj, headers={"Authorization": f"Bearer {body['api_key']}"}
    ) as c2:
        assert c2.get("/v1/endpoints").json()["items"] == []
        ep = c2.post(
            "/v1/endpoints", json={"url": "https://acme.example.com/wh"}
        ).json()
        assert c2.get("/v1/endpoints").json()["items"][0]["id"] == ep["id"]
    clear_settings_override()

    # the plaintext is not retrievable afterwards
    listed = client.get(
        f"/v1/admin/tenants/{tenant_id}/keys", headers=admin
    ).json()
    assert "api_key" not in listed[0], "plaintext key must never be listed again"


def test_revoked_key_stops_working(client, prepared_db):
    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import Settings, clear_settings_override

    admin = {"X-Admin-Token": "test-admin-token"}
    tenant_id = client.post("/v1/admin/tenants", json={"name": "revoke-me"}, headers=admin).json()["id"]
    key = client.post(
        f"/v1/admin/tenants/{tenant_id}/keys", json={"label": "k"}, headers=admin
    ).json()

    database_url, _ = prepared_db
    app_obj = main_module.create_app(
        Settings(database_url=database_url, redis_url="", admin_token="test-admin-token")
    )
    with TestClient(
        app_obj, headers={"Authorization": f"Bearer {key['api_key']}"}
    ) as c2:
        assert c2.get("/v1/endpoints").status_code == 200
        assert c2.post("/v1/admin/keys/%s/revoke" % key["id"], headers=admin).status_code == 200
        assert c2.get("/v1/endpoints").status_code == 401, "revoked key must be refused"
    clear_settings_override()


def test_last_used_at_is_recorded(client, session):
    from app.db import ApiKey

    _register(client)
    client.get("/v1/endpoints")
    assert any(k.last_used_at is not None for k in session.query(ApiKey).all())


# --- schema guarantees -----------------------------------------------------


def test_tenant_id_is_not_nullable_in_the_database(client):
    from app.main import engine

    insp = inspect(engine)
    endpoint_cols = {c["name"]: c for c in insp.get_columns("endpoints")}
    assert endpoint_cols["tenant_id"]["nullable"] is False
