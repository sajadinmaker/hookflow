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

## Authentication and tenants

Machine callers present `Authorization: Bearer hf_<prefix>_<secret>`.

- **Only a SHA-256 digest is stored.** The plaintext is returned once, at
  creation, and is unrecoverable. A fast hash is correct here because the key is
  256 bits of `secrets.token_hex` output — there is no dictionary to attack, so
  bcrypt/argon2 would add per-request latency without adding resistance. That
  trade-off flips immediately if keys are ever user-chosen.
- **Lookup is by `key_prefix`**, a unique column, so authentication is a single
  index hit. The digest comparison uses `hmac.compare_digest`.
- **Unknown prefix and wrong secret return the same error**, so responses do
  not reveal which prefixes exist.
- **Every authorised request resolves to one tenant**, applied as a filter on
  every query. `endpoints.tenant_id` is `NOT NULL` with a foreign key.
- **Cross-tenant access is `404`, never `403`.** A `403` would confirm that an
  id exists, which is a leak in itself. Covered by tests using two tenants.
- **The admin surface fails closed.** With `HOOKFLOW_ADMIN_TOKEN` unset, tenant
  and key creation return `503`. An unauthenticated route that can mint
  credentials is a vulnerability, not a bootstrap mechanism.

`Principal.require_tenant()` is a fail-closed backstop for any handler that
forgets to filter: it raises rather than returning another tenant's row.

## What does NOT exist (honest gaps)

- **API keys cannot expire.** They can be revoked, but there is no TTL or
  rotation schedule, and a leaked key stays valid until someone notices.
- **No per-tenant API rate limits.** Ingest is unbounded per tenant; only
  per-endpoint *delivery* is rate limited.
- **One shared admin token.** No per-operator identity, roles, or audit trail
  of who created a tenant.
- **The rate limiter is per-process when Redis is off**, so it is single-replica
  only. The delivery claim is replica-safe; the limiter is not.
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
