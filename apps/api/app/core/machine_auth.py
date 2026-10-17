"""Machine credential + enrollment-token primitives and HMAC verification.

TRUST DECISION (resolving a contract contradiction)
----------------------------------------------------
The contract originally stated that ``machine_secret`` is "stored only as a
SHA-256 hash" AND that the server "recompute[s] HMAC with stored secret". These
are mutually exclusive: SHA-256 is one-way, so a server holding only the hash
cannot recompute an HMAC keyed on the secret.

Resolution (documented in docs/api/phase2-provider-protocol.md):

* The machine credential returned once to the agent is ``machine_secret`` of the
  form ``msk_<signing_key>``.
* The HMAC key is the ``signing_key`` — the random component after the ``msk_``
  prefix. The server persists ``signing_key`` and treats it as a secret at rest
  (it is the one piece of material that MUST be recoverable to verify
  signatures). The raw ``machine_secret`` string itself is never stored.
* For audit parity with the "stored hashed" requirement, the server also stores
  ``secret_hash = SHA-256(machine_secret)``.

Verification recomputes ``HMAC-SHA256(signing_key, canonical)`` and compares in
constant time, and enforces a ±SKEW timestamp window to defeat replays.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

MACHINE_SECRET_PREFIX = "msk_"
ENROLLMENT_TOKEN_PREFIX = "enr_"


# ---------------------------------------------------------------------------
# Enrollment tokens
# ---------------------------------------------------------------------------
def generate_enrollment_token(nbytes: int = 32) -> str:
    """Return an ``enr_<urlsafe-random>`` token shown to the provider once."""
    return f"{ENROLLMENT_TOKEN_PREFIX}{secrets.token_urlsafe(nbytes)}"


def hash_enrollment_token(token: str) -> str:
    """SHA-256 of an enrollment token, for DB lookup by hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Machine credentials
# ---------------------------------------------------------------------------
def generate_machine_credential(nbytes: int = 32) -> tuple[str, str, str]:
    """Return ``(machine_secret, signing_key, secret_hash)``.

    ``machine_secret`` (``msk_<signing_key>``) is returned to the agent exactly
    once. ``signing_key`` is persisted as the HMAC key; ``secret_hash`` is the
    SHA-256 of ``machine_secret`` for audit parity.
    """
    signing_key = secrets.token_urlsafe(nbytes)
    machine_secret = f"{MACHINE_SECRET_PREFIX}{signing_key}"
    secret_hash = hashlib.sha256(machine_secret.encode("utf-8")).hexdigest()
    return machine_secret, signing_key, secret_hash


def signing_key_from_secret(machine_secret: str) -> str:
    """Extract the HMAC ``signing_key`` from a full ``msk_`` machine secret."""
    if machine_secret.startswith(MACHINE_SECRET_PREFIX):
        return machine_secret[len(MACHINE_SECRET_PREFIX):]
    return machine_secret


# ---------------------------------------------------------------------------
# HMAC heartbeat signing / verification
# ---------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_canonical(machine_id: str, timestamp: str, raw_body: bytes) -> str:
    """Canonical signing string: ``<machine_id>\\n<timestamp>\\n<sha256_hex(body)>``.

    No trailing newline (exact per contract).
    """
    return f"{machine_id}\n{timestamp}\n{sha256_hex(raw_body)}"


def compute_signature(signing_key: str, canonical: str) -> str:
    return hmac.new(
        signing_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_signature(signing_key: str, canonical: str, provided_signature: str) -> bool:
    """Constant-time comparison of the recomputed HMAC against the provided one."""
    expected = compute_signature(signing_key, canonical)
    return hmac.compare_digest(expected, provided_signature or "")
