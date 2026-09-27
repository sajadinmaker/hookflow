# HookFlow security

## What exists

- Per-endpoint HMAC-SHA256 signatures (`X-Hookflow-Signature: sha256=...` over
  the raw JSON body). Receivers verify with `app.security.verify_signature`,
  which uses `hmac.compare_digest` rather than `==` so verification does not
  leak the secret through timing.
- **Secrets encrypted at rest.** Stored value is a Fernet envelope,
  `enc:v1:<token>`. Plaintext exists in memory only for the duration of one
  signing call and is never logged or written back.
- **Generated secrets are disclosed exactly once**, in the `POST /v1/endpoints`
  response. There is deliberately no endpoint that returns a secret again, and
  listing endpoints never includes one. (Before this existed, a
  server-generated secret was never returned at all, which left receivers
  unable to verify signatures — the encryption work surfaced it.)
- Input validation: endpoint URL must be http(s), ≤ 2000 chars; payload must
  be JSON-serialisable and ≤ `max_body_bytes` (default 256KB); idempotency keys
  ≤ 128 chars; pagination bounds enforced.
- No secrets in code: `Settings` reads `.env`; `.env.example` holds only
  non-secret defaults. `.gitignore` excludes `.env` and `*.db`.

## Why encryption rather than hashing

A webhook secret is a *shared* secret: the receiver verifies a signature that
HookFlow produces. That means the server must be able to reproduce the exact
secret bytes when signing. A one-way hash can verify an inbound credential but
cannot sign an outbound payload — hashing the stored value would break delivery
outright. Hence authenticated encryption (AES-128-CBC + HMAC-SHA256 via Fernet),
not a hash.

## Key management

`app/crypto.py` exposes a `KeyProvider` protocol with one method, returning a
`Fernet`. `StaticKeyProvider` reads key material from configuration. Swapping
in AWS KMS or Vault means implementing that one method: no call site and no
stored value changes.

- `SECRET_ENCRYPTION_KEY` accepts a real Fernet key or any passphrase
  (SHA-256 stretched).
- Unset, a published **development key** is used so local SQLite files survive
  a restart. `/ready` then reports `secret_encryption: development-key`, so a
  misconfigured production deploy is visible rather than silent.
- The `enc:v1:` prefix is what makes rotation possible: a `v2` format can be
  introduced and rows migrated, rather than every stored secret becoming
  undecryptable at once. `SecretBox.reencrypt` re-wraps a value under the
  current key; rows written before encryption are still readable and are logged
  as needing migration.
- Unknown envelope versions are **rejected**, not treated as plaintext. A
  future `enc:v2:` row must fail loudly rather than be silently used as a
  signing secret.

## What does NOT exist (honest gaps)

- **No multi-tenant auth.** Anyone with network access can register endpoints
  and ingest. Endpoint secrets authenticate *receivers*, not *producers*. Fix:
  tenants and API keys.
- **A single worker replica only.** Due deliveries are selected without being
  claimed, so two workers can both fetch and POST the same delivery. Correctness
  issue, not a security one, but it is the most important open bug.
- No TLS in Compose: terminate TLS at a reverse proxy / load balancer.
- Rate limits are per-endpoint delivery fairness, not anti-abuse on ingest.
- `/metrics` is unauthenticated. Fine on a private network; do not expose it
  publicly without a gate.

## Receiver verification (Python)

```python
import hmac, hashlib
expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-Hookflow-Signature"])
```

Verify against the **raw request body**, before any JSON parsing or
re-serialisation — a re-encoded body will not produce the same digest.
