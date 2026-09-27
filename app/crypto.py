"""Encryption for webhook signing secrets at rest.

Why encryption and not hashing: a webhook secret is a *shared* secret. The
receiver verifies a signature that we produce, so the server must be able to
reproduce the exact secret bytes at signing time. A one-way hash can verify an
inbound credential but can never sign an outbound payload, so hashing the stored
value would break delivery outright.

What is stored is an authenticated-encrypted token (Fernet: AES-128-CBC +
HMAC-SHA256), never the raw secret. Plaintext exists only in memory, for the
duration of a single signing operation.

Key handling sits behind :class:`KeyProvider`, whose only job is to hand back a
:class:`~cryptography.fernet.Fernet`. A KMS-backed provider (AWS/GCP KMS, Vault)
would fetch or unwrap a data key and return a Fernet for it; no call site or
stored format changes when that happens.

Stored format: ``enc:v1:<token>``. The version prefix is what makes a future key
rotation or algorithm change possible — a ``v2`` prefix can be introduced and
rows migrated, instead of every stored secret becoming undecryptable at once.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("hookflow.crypto")

SCHEME_PREFIX = "enc:"
ENVELOPE_PREFIX = SCHEME_PREFIX + "v1:"
SUPPORTED_VERSIONS = frozenset({"v1"})

# Used only when no key is configured, so a developer working against a local
# SQLite file does not lose every secret on restart. This key is public by
# construction and must never protect real data; /ready reports when it is in
# use so a misconfigured production deploy is loud rather than silent.
DEV_KEY_MATERIAL = b"hookflow-insecure-development-key-do-not-use-in-production"


class DecryptionError(RuntimeError):
    """A stored secret could not be decrypted (wrong key, or the value was altered)."""


class KeyProvider(Protocol):
    """Supplies the symmetric key protecting webhook secrets.

    The single method is the entire swap point for an external key manager.
    """

    def fernet(self) -> Fernet:  # pragma: no cover - protocol definition
        ...


def normalize_key(material: str | bytes) -> bytes:
    """Coerce operator-supplied key material into a valid Fernet key.

    A Fernet key is 32 urlsafe-base64 bytes. Anything else is stretched with
    SHA-256 so an operator can paste a passphrase directly.
    """
    raw = material.encode() if isinstance(material, str) else material
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


class StaticKeyProvider:
    """Key material supplied by configuration (env var, or a mounted secret)."""

    def __init__(self, material: str | bytes) -> None:
        self._material = material
        self._fernet = Fernet(normalize_key(material))

    def fernet(self) -> Fernet:
        return self._fernet


def is_encrypted(stored: str) -> bool:
    """True when the value carries the `enc:` envelope scheme at all.

    Deliberately does not check the version: a value from an unknown future
    version is still ciphertext, and must never be mistaken for plaintext.
    """

    return stored.startswith(SCHEME_PREFIX)


def _split_envelope(stored: str) -> tuple[str, str]:
    """Split ``enc:<version>:<token>`` into ``(version, token)``.

    The envelope has two colons, so this cannot be a single partition() — that
    would yield the literal scheme "enc" as the version.
    """
    _, separator, remainder = stored.partition(":")
    if not separator:
        raise DecryptionError(f"unrecognised secret envelope {stored!r}")
    version, separator, token = remainder.partition(":")
    if not separator or not token:
        raise DecryptionError("malformed secret envelope: missing version or token")
    return version, token


class SecretBox:
    """Encrypts and decrypts webhook secrets. Construct once, reuse everywhere."""

    def __init__(self, provider: KeyProvider, *, using_development_key: bool = False) -> None:
        self._provider = provider
        self.using_development_key = using_development_key

    def encrypt(self, plaintext: str) -> str:
        token = self._provider.fernet().encrypt(plaintext.encode())
        return ENVELOPE_PREFIX + token.decode()

    def decrypt(self, stored: str) -> str:
        if not is_encrypted(stored):
            # Row predates encryption. Return as-is so existing installs keep
            # delivering; a re-encryption job can migrate these in place.
            logger.warning("secret stored without encryption; re-encrypt to finish migration")
            return stored
        version, token = _split_envelope(stored)
        if version not in SUPPORTED_VERSIONS:
            raise DecryptionError(f"unsupported secret envelope version {version!r}")
        try:
            raw = self._provider.fernet().decrypt(token.encode())
        except InvalidToken as exc:
            raise DecryptionError(
                "cannot decrypt secret: wrong encryption key, or the value was altered"
            ) from exc
        return raw.decode()

    def reencrypt(self, stored: str) -> str:
        """Re-wrap an existing value under the currently configured key."""
        return self.encrypt(self.decrypt(stored))


def build_secret_box(configured_key: str) -> SecretBox:
    """Construct the SecretBox for the current configuration.

    An unset key falls back to a published development key. That is a
    convenience for local work, never something to ship: the fallback is
    detectable via :attr:`SecretBox.using_development_key` and is reported by
    the readiness probe.
    """
    if configured_key:
        return SecretBox(StaticKeyProvider(configured_key))
    return SecretBox(StaticKeyProvider(DEV_KEY_MATERIAL), using_development_key=True)


@lru_cache(maxsize=1)
def _cached_secret_box(configured_key: str) -> SecretBox:
    return build_secret_box(configured_key)


def get_secret_box(configured_key: str | None = None) -> SecretBox:
    """Process-wide SecretBox. Pass an explicit key to bypass the cache."""
    if configured_key is not None:
        return build_secret_box(configured_key)
    from .config import get_settings

    return _cached_secret_box(get_settings().secret_encryption_key)


def reset_secret_box_cache() -> None:
    _cached_secret_box.cache_clear()
