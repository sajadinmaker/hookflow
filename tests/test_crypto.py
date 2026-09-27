"""Secret handling: encryption at rest, single disclosure, and signing correctness."""

import pytest

from app.crypto import (
    ENVELOPE_PREFIX,
    DecryptionError,
    SecretBox,
    StaticKeyProvider,
    build_secret_box,
    is_encrypted,
    normalize_key,
)


# --- unit level ------------------------------------------------------------


def test_encrypt_decrypt_round_trip():
    box = SecretBox(StaticKeyProvider("a-test-key"))
    secret = "s3cr3t-value"
    stored = box.encrypt(secret)
    assert stored != secret, "ciphertext must not equal plaintext"
    assert stored.startswith(ENVELOPE_PREFIX)
    assert box.decrypt(stored) == secret


def test_ciphertext_is_not_reversible_without_the_key():
    box = SecretBox(StaticKeyProvider("key-one"))
    stored = box.encrypt("top-secret")
    other = SecretBox(StaticKeyProvider("key-two"))
    with pytest.raises(DecryptionError):
        other.decrypt(stored)


def test_tampered_ciphertext_is_rejected():
    box = SecretBox(StaticKeyProvider("a-test-key"))
    stored = box.encrypt("top-secret")
    prefix, _, token = stored.partition(":")
    flipped = "A" if token[-1] != "A" else "B"
    with pytest.raises(DecryptionError):
        box.decrypt(f"{prefix}:{token[:-1]}{flipped}")


def test_legacy_plaintext_secret_still_delivers():
    """Rows written before encryption existed must keep working after rollout."""
    box = SecretBox(StaticKeyProvider("a-test-key"))
    assert box.decrypt("raw-legacy-secret") == "raw-legacy-secret"
    assert not is_encrypted("raw-legacy-secret")


def test_legacy_secret_can_be_reencrypted_in_place():
    box = SecretBox(StaticKeyProvider("a-test-key"))
    migrated = box.reencrypt("raw-legacy-secret")
    assert is_encrypted(migrated)
    assert box.decrypt(migrated) == "raw-legacy-secret"


def test_unknown_envelope_version_is_refused():
    box = SecretBox(StaticKeyProvider("a-test-key"))
    with pytest.raises(DecryptionError):
        box.decrypt("enc:v9:whatever")


def test_fernet_key_and_passphrase_both_accepted():
    """Operators can paste either a real Fernet key or a plain passphrase."""
    from cryptography.fernet import Fernet

    real_fernet_key = Fernet.generate_key().decode()
    assert SecretBox(StaticKeyProvider(real_fernet_key)).decrypt(
        SecretBox(StaticKeyProvider(real_fernet_key)).encrypt("x")
    ) == "x"
    assert len(normalize_key("short-passphrase")) == 44


def test_missing_key_falls_back_to_development_key_and_is_flagged():
    dev = build_secret_box("")
    assert dev.using_development_key is True
    configured = build_secret_box("a-real-key")
    assert configured.using_development_key is False
    # A dev-key box must not be able to read a properly-keyed secret.
    with pytest.raises(DecryptionError):
        dev.decrypt(configured.encrypt("x"))


# --- application level -----------------------------------------------------


def _register(client, url="https://example.com/wh", **kw):
    return client.post("/v1/endpoints", json={"url": url, **kw}).json()


def test_secret_is_encrypted_at_rest(client):
    from app.main import SessionLocal
    from app.db import Endpoint

    plaintext = "super-secret-value-123"
    created = _register(client, secret=plaintext)

    session = SessionLocal()
    try:
        row = session.get(Endpoint, created["id"])
        stored = row.secret
    finally:
        session.close()

    assert stored != plaintext, "plaintext secret reached the database"
    assert is_encrypted(stored)


def test_generated_secret_is_returned_exactly_once(client):
    """A server-generated secret is useless unless the receiver receives it."""
    created = _register(client)  # no secret supplied -> server generates one
    assert created["secret"], "generated secret must be disclosed on creation"

    listed = client.get("/v1/endpoints").json()["items"]
    assert all("secret" not in item for item in listed), "listing must not disclose secrets"


def test_returned_secret_verifies_the_delivery_signature(client):
    """End-to-end: the disclosed secret must reproduce the outbound HMAC."""
    import hashlib
    import hmac

    import httpx

    from app.config import Settings
    from app.db import Delivery, Endpoint
    from app.main import SessionLocal
    from app.worker import process_due_deliveries
    from app.ratelimit import RateLimiter

    from app.security import SIGNATURE_HEADER

    created = _register(client, secret="shared-signing-secret")
    client.post("/v1/events", json={"endpoint_id": created["id"], "payload": {"n": 1}})

    seen: dict[str, str] = {}

    def handler(request):
        seen["sig"] = request.headers[SIGNATURE_HEADER]
        seen["body"] = request.content
        return httpx.Response(200)

    session = SessionLocal()
    try:
        delivery = session.query(Delivery).first()
        endpoint = session.get(Endpoint, delivery.endpoint_id)
        body = delivery.event.payload.encode()
        process_due_deliveries(
            session,
            Settings(database_url="sqlite://", redis_url="", secret_encryption_key=""),
            RateLimiter(),
            httpx.Client(transport=httpx.MockTransport(handler)),
        )
    finally:
        session.close()

    expected = "sha256=" + hmac.new(
        b"shared-signing-secret", seen["body"], hashlib.sha256
    ).hexdigest()
    assert seen["sig"] == expected, "signature was not produced with the plaintext secret"
