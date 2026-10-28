"""Customer-side encryption for Proof-of-Privacy (ADR-0005).

Everything sensitive happens on the customer's machine before upload:

1. a fresh 32-byte Data Key encrypts the job input (ChaCha20-Poly1305);
2. the Data Key is split k-of-n (Shamir) and each share is sealed to ONE guardian's X25519 key
   (fetch them from ``GET /v1/privacy/verifier-key``), so the platform stores only envelopes;
3. the result comes back sealed to the customer's own X25519 key.

Typical use::

    priv, pub = generate_customer_keypair()          # keep priv secret, reuse across jobs
    keys = client_get("/v1/privacy/verifier-key")     # {"guardians": {...}, "threshold": 2, ...}
    body = prepare_private_job({"prompt": "..."}, guardians=keys["guardians"],
                               threshold=keys["threshold"], result_public_key=pub)
    body.update(workload_type="text-generation", model="...", isolation_tier="sealed")
    # POST /v1/jobs with body; later:
    result = decrypt_job_output(job["encrypted_output"], priv, job_id=job["id"])
"""

from __future__ import annotations

import hashlib
import json
import secrets
import warnings
from typing import Any, Optional

from .privacy_crypto import (
    INPUT_AAD,
    canonical_json,
    encrypt_data,
    generate_x25519_keypair,
    open_sealed,
    output_aad,
    seal,
    share_aad,
    split_secret,
)


# --------------------------------------------------------------------------- guardian key pinning
#
# The guardian keys come from the platform (GET /v1/privacy/verifier-key). A platform that served
# keys it controls could open every Data Key, so ComputeCoin publishes the FINGERPRINT of the
# guardian set somewhere it cannot quietly change (docs, release notes, a DNS record, partners'
# own sites). Pinning that fingerprint makes the client refuse any other guardian set.

GUARDIAN_MANIFEST_VERSION = "ccpp-guardians-v1"


class GuardianPinError(ValueError):
    """The guardian keys served do not match the pinned fingerprint (or none is pinned in strict mode)."""


class UnpinnedGuardianWarning(UserWarning):
    """Guardian keys were taken from the platform without a pinned fingerprint to check them against."""


def guardian_manifest(guardians: dict[str, str], threshold: int) -> dict[str, Any]:
    return {"v": GUARDIAN_MANIFEST_VERSION, "threshold": int(threshold),
            "guardians": {str(n): str(k).lower() for n, k in guardians.items()}}


def guardian_manifest_fingerprint(guardians: dict[str, str], threshold: int) -> str:
    """SHA-256 of the canonical guardian manifest: what ComputeCoin publishes and customers pin."""
    return hashlib.sha256(canonical_json(guardian_manifest(guardians, threshold))).hexdigest()


def check_guardian_pin(
    guardians: dict[str, str], threshold: int, pinned_fingerprint: Optional[str], *, require_pin: bool = False
) -> None:
    """Refuse a guardian set that does not match the pin. Without a pin: warn, or refuse when strict."""
    if not pinned_fingerprint:
        if require_pin:
            raise GuardianPinError("no guardian fingerprint is pinned (require_guardian_pin is set)")
        warnings.warn(
            "guardian keys were not checked against a pinned fingerprint; you are trusting the platform "
            "to serve the right ones (set guardian_pin / COMPUTECOIN_GUARDIAN_PIN)",
            UnpinnedGuardianWarning, stacklevel=3,
        )
        return
    actual = guardian_manifest_fingerprint(guardians, threshold)
    if actual != pinned_fingerprint.strip().lower():
        raise GuardianPinError(f"guardian keys do not match the pinned fingerprint (got {actual})")


def generate_customer_keypair() -> tuple[str, str]:
    """Return (private_hex, public_hex). Results are sealed to the public key; keep the private key secret."""
    return generate_x25519_keypair()


def encrypt_job_input(input_dict: dict[str, Any]) -> tuple[dict[str, str], bytes]:
    """Encrypt the input with a fresh Data Key. Returns (encrypted_input, data_key)."""
    data_key = secrets.token_bytes(32)
    return encrypt_data(data_key, json.dumps(input_dict, sort_keys=True).encode("utf-8"), INPUT_AAD), data_key


def split_data_key(data_key: bytes, n: int = 3, k: int = 2) -> list[dict[str, Any]]:
    """Raw Shamir shares. Never send these: use :func:`seal_shares_for_guardians`."""
    return [{"x": x, "share_hex": s.hex()} for x, s in split_secret(data_key, n=n, k=k)]


def seal_shares_for_guardians(data_key: bytes, guardians: dict[str, str], threshold: int) -> dict[str, Any]:
    """Split the Data Key across every guardian and seal each share to that guardian only."""
    if threshold < 2 or len(guardians) < threshold:
        raise ValueError("need at least `threshold` (>= 2) guardians")
    names = sorted(guardians)
    shares = split_secret(data_key, n=len(names), k=threshold)
    return {name: {"x": x, "envelope": seal(guardians[name], share, share_aad(name))} for name, (x, share) in zip(names, shares)}


def prepare_private_job(
    input_dict: dict[str, Any],
    *,
    guardians: dict[str, str],
    threshold: int,
    result_public_key: str,
    data_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The privacy fields of a ``POST /v1/jobs`` body. The plaintext input is not included."""
    encrypted_input, data_key = encrypt_job_input(input_dict)
    return {
        "input": {},
        "encrypted_input": encrypted_input,
        "dk_shares": seal_shares_for_guardians(data_key, guardians, threshold),
        "data_policy": {**(data_policy or {}), "result_public_key": result_public_key},
    }


def decrypt_job_output(encrypted_output: dict[str, Any], customer_private_key: str, *, job_id: str) -> dict[str, Any]:
    """Open a result sealed to the customer. ``job_id`` is bound in, so a result cannot be swapped between jobs."""
    return json.loads(open_sealed(customer_private_key, encrypted_output, output_aad(job_id)).decode("utf-8"))
