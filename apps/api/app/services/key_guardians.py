"""Key Guardians: threshold release of a job's Data Key (ADR-0005 section 5.5).

The customer's SDK splits the Data Key with Shamir (k-of-n) and seals each share to ONE
guardian's X25519 public key. The platform stores only those sealed envelopes, so the database
never holds a usable share.

At release time each guardian whose private key is held by this process:
  1. re-checks the signed attestation verdict and the job's policy,
  2. writes a KEY_RELEASE entry to the transparency log (log-before-release),
  3. opens its own envelope and re-seals the share to the host's in-RAM X25519 key, bound to
     this job (AAD), so only that machine's RAM can open it and only for this job.

The boot guard requires a production process to hold FEWER guardian keys than the threshold, so
the platform alone cannot rebuild a Data Key: the remaining shares must come from guardians run
elsewhere (a partner, or the customer). That external guardian service is not built yet; until it
is, release fails closed with ``insufficient_guardians_available`` outside development.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.jobs import Job
from app.models.privacy import TrustedImage
from app.models.provider import ProviderMachine
from app.services.attestation_verifier import PRIVACY_TIERS, TIER_RANK, verdict_is_current, verify_verdict_signature
from app.services.privacy_crypto import (
    PrivacyCryptoError,
    is_sealed_envelope,
    open_sealed,
    reconstruct_secret,
    release_aad,
    seal,
    share_aad,
    split_secret,
)
from app.services.privacy_keys import (
    PrivacyConfigError,
    guardian_private_keys,
    guardian_public_keys,
    guardian_threshold,
    require_privacy_active,
)
from app.services.transparency_log import append_transparency_log_entry

__all__ = [
    "GuardianReleaseError",
    "release_guardian_shares",
    "validate_sealed_shares",
    "split_secret",
    "reconstruct_secret",
]


class GuardianReleaseError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def validate_sealed_shares(dk_shares: Any) -> None:
    """Shape check at job submission: one sealed envelope per known guardian, no raw shares."""
    try:
        known = guardian_public_keys()
        k = guardian_threshold()
    except PrivacyConfigError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(dk_shares, dict) or not dk_shares:
        raise ValueError("dk_shares must map guardian names to sealed shares")
    xs = set()
    for name, entry in dk_shares.items():
        if name not in known:
            raise ValueError(f"dk_shares: unknown guardian {name!r}")
        if not isinstance(entry, dict) or set(entry) != {"x", "envelope"}:
            raise ValueError(f"dk_shares[{name!r}] must be {{'x': int, 'envelope': sealed}} and nothing else")
        if not isinstance(entry["x"], int) or not 1 <= entry["x"] <= 255 or entry["x"] in xs:
            raise ValueError(f"dk_shares[{name!r}].x must be a distinct integer in 1..255")
        xs.add(entry["x"])
        if not is_sealed_envelope(entry["envelope"]):
            raise ValueError(f"dk_shares[{name!r}].envelope is not a sealed ccpp-v1 envelope")
    if len(dk_shares) < k:
        raise ValueError(f"dk_shares must contain at least {k} guardians")


async def release_guardian_shares(
    db: AsyncSession,
    *,
    job: Job,
    machine: ProviderMachine,
    attestation_verdict: dict[str, Any],
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Release ``k`` shares re-sealed to the verdict's host key. Raises GuardianReleaseError."""
    ts = now or utcnow()
    try:
        require_privacy_active()
        private_keys = guardian_private_keys()
        k = guardian_threshold()
    except PrivacyConfigError as exc:
        raise GuardianReleaseError("privacy_not_configured", str(exc)) from exc

    v = attestation_verdict
    if not verify_verdict_signature(v):
        raise GuardianReleaseError("verdict_signature_invalid", "Attestation verdict signature is invalid")
    if not verdict_is_current(v, ts):
        raise GuardianReleaseError("verdict_expired", "Attestation verdict has expired")
    if v["machine_id"] != str(machine.id):
        raise GuardianReleaseError("machine_mismatch", "Verdict machine does not match target machine")
    if machine.is_banned:
        raise GuardianReleaseError("machine_is_banned", "Machine is banned")
    if job.machine_id != machine.id:
        raise GuardianReleaseError("job_not_assigned_to_machine", "The job is not assigned to this machine")

    req_tier = (job.isolation_tier or "open").lower()
    reached = str(v["tier_reached"]).lower()
    if req_tier not in PRIVACY_TIERS:
        raise GuardianReleaseError("not_a_privacy_job", "Only Sealed, Sealed Pro and Confidential jobs use guardians")
    if TIER_RANK.get(reached, -1) < TIER_RANK[req_tier]:
        raise GuardianReleaseError("isolation_tier_insufficient", f"machine reached {reached!r}, job requires {req_tier!r}")

    image = (await db.execute(select(TrustedImage).where(TrustedImage.image_hash == v["image_hash"]))).scalar_one_or_none()
    if image is None or image.status != "active":
        raise GuardianReleaseError("image_not_trusted", "The verdict's OS image is not an active trusted image")

    policy = job.data_policy or {}
    job_var = job.value_at_risk if job.value_at_risk is not None else decimal.Decimal("0")
    stake = machine.stake_bonded or decimal.Decimal("0")
    if req_tier in ("sealed", "sealed_pro") and job_var > stake:
        raise GuardianReleaseError("value_at_risk_exceeds_stake", f"value at risk {job_var} exceeds bonded stake {stake}")
    allowed = policy.get("allowed_regions")
    if allowed and (not machine.region or machine.region not in allowed):
        raise GuardianReleaseError("region_policy_violation", f"machine region {machine.region!r} is not allowed")

    shares = job.dk_shares or {}
    available = [g for g in guardian_public_keys() if g in private_keys and g in shares]
    if len(available) < k:
        raise GuardianReleaseError(
            "insufficient_guardians_available",
            f"{len(available)} of {k} required guardians can release here; the rest must come from external guardians",
        )

    released: list[dict[str, Any]] = []
    for name in available[:k]:
        entry = shares[name]
        try:
            share = open_sealed(private_keys[name], entry["envelope"], share_aad(name))
        except (PrivacyCryptoError, KeyError, TypeError) as exc:
            raise GuardianReleaseError("share_unreadable", f"guardian {name}: sealed share does not open") from exc
        log = await append_transparency_log_entry(
            db,
            "KEY_RELEASE",
            {
                "guardian": name,
                "job_id": str(job.id),
                "machine_id": str(machine.id),
                "verdict_id": v["verdict_id"],
                "share_index": entry["x"],
                "released_at": ts.isoformat(),
            },
        )
        released.append(
            {
                "guardian": name,
                "share_index": entry["x"],
                "wrapped_share": seal(v["ephemeral_public_key"], share, release_aad(str(job.id), name)),
                "log_leaf_index": log.leaf_index,
                "log_leaf_hash": log.leaf_hash,
            }
        )
    return released
