"""Attestation Verifier for Proof-of-Privacy (ADR-0005).

Turns a host's attestation evidence into a signed, short-lived **Attestation Verdict** that
names the machine, the OS image it booted, the tier it reached, and the two keys generated
inside its RAM (X25519 for receiving Data Key shares, Ed25519 for signing its wipe receipt).

Verdicts are signed with Ed25519. The verifier's public key is published, so anyone can check
a verdict offline and nobody without the private key can forge one.

What is and is not verified (stated plainly, ADR-0005 section 3):
the caller (the attestation route) has already checked nonce freshness, the quote's binding to
the nonce and measurements, and the machine's signature. That "quote" is produced in software
with a key the host itself holds, so it does NOT prove the measurements came from a TPM.
Real TPM quote verification (vendor EK certificate chain, AK binding, PCR quote) and TDX/SEV-SNP
+ NVIDIA report verification are not implemented. Until they are, this module refuses to issue
verdicts unless ``PRIVACY_SOFTWARE_ATTESTATION`` is on, which the boot guard forbids outside
development and tests.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.privacy import TransparencyLogLeaf, TrustedImage
from app.models.provider import ProviderMachine
from app.services.privacy_crypto import canonical_json, is_hex32, sign, verify
from app.services.privacy_keys import PrivacyConfigError, require_privacy_active, verifier_public_key, verifier_signing_key
from app.services.transparency_log import append_transparency_log_entry

MIN_SEALED_STAKE = decimal.Decimal("1000.00")
MIN_SEALED_PRO_STAKE = decimal.Decimal("5000.00")
VERDICT_TTL = dt.timedelta(minutes=30)

PRIVACY_TIERS = ("sealed", "sealed_pro", "confidential")
TIER_RANK = {"open": 0, "container": 0, "gvisor": 1, "microvm": 2, "sealed": 3, "sealed_pro": 4, "confidential": 5}
# Which registered OS image types satisfy a requested tier.
_IMAGE_TIERS = {"sealed": ("sealed", "sealed_pro"), "sealed_pro": ("sealed", "sealed_pro"), "confidential": ("confidential",)}
# Locks the Sealed OS must report as active. Missing is a failure, never a default "yes".
REQUIRED_SECURITY_STATE = ("bare_metal", "iommu_enabled", "kernel_lockdown")

VERDICT_FIELDS = (
    "verdict_id",
    "machine_id",
    "image_hash",
    "tier_reached",
    "ephemeral_public_key",
    "receipt_signing_public_key",
    "nonce",
    "issued_at",
    "expires_at",
    "verifier_public_key",
)


class AttestationVerifierError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def verdict_signing_bytes(verdict: dict[str, Any]) -> bytes:
    missing = [f for f in VERDICT_FIELDS if f not in verdict]
    if missing:
        raise ValueError(f"verdict is missing {', '.join(missing)}")
    return canonical_json({f: verdict[f] for f in VERDICT_FIELDS})


def sign_verdict(verdict: dict[str, Any]) -> str:
    """Sign with the platform verifier key. ``verdict['verifier_public_key']`` must name that key."""
    if verdict.get("verifier_public_key") != verifier_public_key():
        raise ValueError("verdict names a different verifier key")
    return sign(verifier_signing_key(), verdict_signing_bytes(verdict))


def verify_verdict_signature(verdict: dict[str, Any], verifier_public_hex: str | None = None) -> bool:
    """True only if the verdict is complete and signed by the expected verifier key."""
    expected_key = verifier_public_hex or verifier_public_key()
    if not isinstance(verdict, dict) or verdict.get("verifier_public_key") != expected_key:
        return False
    try:
        message = verdict_signing_bytes(verdict)
    except ValueError:
        return False
    return verify(expected_key, message, verdict.get("signature", ""))


def verdict_is_current(verdict: dict[str, Any], now: dt.datetime) -> bool:
    try:
        expires = dt.datetime.fromisoformat(str(verdict["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    return now < expires


def _pcrs_match(expected: dict[str, str], reported: dict[str, str]) -> bool:
    if not expected:
        return False
    norm = {k.lower(): str(v).lower() for k, v in reported.items()}
    return all(norm.get(k.lower()) == str(v).lower() for k, v in expected.items())


async def _matching_image(db: AsyncSession, tier: str, pcrs: dict[str, str]) -> TrustedImage | None:
    res = await db.execute(
        select(TrustedImage).where(TrustedImage.status == "active", TrustedImage.tier.in_(_IMAGE_TIERS[tier]))
    )
    for image in res.scalars().all():
        if _pcrs_match(image.expected_pcrs or {}, pcrs):
            return image
    return None


async def verify_machine_evidence(
    db: AsyncSession,
    *,
    machine: ProviderMachine,
    requested_tier: str,
    nonce: str,
    pcr_measurements: dict[str, str],
    ephemeral_public_key: str | None,
    receipt_signing_public_key: str | None,
    security_state: dict[str, Any] | None,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], TransparencyLogLeaf]:
    """Check evidence for a privacy tier and issue a signed verdict, logged before it is returned.

    Precondition: the caller verified the nonce, the quote and the machine signature.
    """
    try:
        require_privacy_active()
    except PrivacyConfigError as exc:
        raise AttestationVerifierError("privacy_tiers_not_enabled") from exc
    if not settings.PRIVACY_SOFTWARE_ATTESTATION:
        raise AttestationVerifierError(
            "hardware_attestation_not_available",
            "Sealed and Confidential tiers need hardware attestation (TPM / TDX / SEV-SNP + NVIDIA), "
            "which is not implemented yet; no verdict can be issued.",
        )
    ts = now or utcnow()
    tier = (requested_tier or "").lower()
    if tier not in PRIVACY_TIERS:
        raise AttestationVerifierError("unsupported_tier", f"{tier!r} is not a privacy tier")
    if machine.is_banned:
        raise AttestationVerifierError("machine_is_banned", "Machine is banned after a proven leak or tamper")
    if not is_hex32(ephemeral_public_key):
        raise AttestationVerifierError("invalid_ephemeral_public_key", "ephemeral_public_key must be a 32-byte X25519 key in hex")
    if not is_hex32(receipt_signing_public_key):
        raise AttestationVerifierError("invalid_receipt_signing_key", "receipt_signing_public_key must be a 32-byte Ed25519 key in hex")

    state = security_state or {}
    failed = [k for k in REQUIRED_SECURITY_STATE if state.get(k) is not True]
    if failed:
        raise AttestationVerifierError("security_state_violation", f"not reported as active: {', '.join(failed)}")

    image = await _matching_image(db, tier, pcr_measurements or {})
    if image is None:
        raise AttestationVerifierError("unknown_os_image", "Boot measurements match no active trusted image for this tier")

    stake = machine.stake_bonded or decimal.Decimal("0")
    if tier == "sealed" and stake < MIN_SEALED_STAKE:
        raise AttestationVerifierError("insufficient_stake_for_sealed", f"bonded {stake}, need {MIN_SEALED_STAKE}")
    if tier == "sealed_pro":
        if stake < MIN_SEALED_PRO_STAKE:
            raise AttestationVerifierError("insufficient_stake_for_sealed_pro", f"bonded {stake}, need {MIN_SEALED_PRO_STAKE}")
        if not machine.memory_encryption_active:
            raise AttestationVerifierError("memory_encryption_not_active", "Sealed Pro needs AMD SME / Intel TME active")
        if not machine.kyc_verified:
            raise AttestationVerifierError("kyc_verification_required", "Sealed Pro needs a KYC-verified host")

    verdict: dict[str, Any] = {
        "verdict_id": str(uuid.uuid4()),
        "machine_id": str(machine.id),
        "image_hash": image.image_hash,
        "tier_reached": tier,
        "ephemeral_public_key": ephemeral_public_key.lower(),
        "receipt_signing_public_key": receipt_signing_public_key.lower(),
        "nonce": nonce,
        "issued_at": ts.isoformat(),
        "expires_at": (ts + VERDICT_TTL).isoformat(),
        "verifier_public_key": verifier_public_key(),
    }
    verdict["signature"] = sign_verdict(verdict)
    leaf = await append_transparency_log_entry(db, "ATTESTATION_VERDICT", verdict)
    return verdict, leaf
