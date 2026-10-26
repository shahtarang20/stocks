"""Canary jobs and leak handling for Proof-of-Privacy (ADR-0005 section 5.7).

A canary is a private job the platform sends to a host, carrying a unique secret marker (for
example a fake API key). It is built exactly like a customer's private job (encrypted input,
guardian-sealed Data Key shares, a result key) so a host cannot tell it apart. If the marker is
ever seen outside, the host that ran it leaked: its bonded stake is recorded as slashed and the
machine is banned.

The database records the slash. Moving the COMPUTE is done on-chain by the ComputeStakeVault
slasher (propose, wait the challenge window, execute); the SlashEvent has no ledger transaction
until then, which the model documents as "recorded but not yet posted".
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.jobs import Job
from app.models.privacy import CanaryJob
from app.models.provider import ProviderMachine
from app.models.reputation import SlashEvent
from app.services.privacy_crypto import INPUT_AAD, encrypt_data, generate_x25519_keypair, seal, share_aad, split_secret
from app.services.privacy_keys import guardian_public_keys, guardian_threshold
from app.services.transparency_log import append_transparency_log_entry

_CENT = decimal.Decimal("0.01")


def generate_canary_token(prefix: str = "sk_live_") -> str:
    """A marker shaped like a real credential, so it is worth stealing and easy to search for."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def token_fingerprint(token: str) -> str:
    """What goes into the public log: a hash, never the token or a prefix of it."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_canary_job(
    db: AsyncSession,
    *,
    machine: ProviderMachine,
    customer_id: uuid.UUID,
    model_version: str,
    canary_type: str = "api_key",
) -> tuple[Job, CanaryJob]:
    """Create a canary that is indistinguishable from a customer's Sealed job."""
    token = generate_canary_token()
    data_key = secrets.token_bytes(32)
    plaintext = json.dumps(
        {"prompt": "Summarize this incident report.", "context": {"service_api_key": token}}, sort_keys=True
    ).encode()
    guardians = guardian_public_keys()
    shares = split_secret(data_key, n=len(guardians), k=guardian_threshold())
    _, result_public_key = generate_x25519_keypair()  # nobody needs to read a canary's result

    job = Job(
        customer_id=customer_id,
        workload_type="text-generation",
        model=model_version,
        input={},
        encrypted_input=encrypt_data(data_key, plaintext, INPUT_AAD),
        dk_shares={
            name: {"x": x, "envelope": seal(pub, share, share_aad(name))}
            for (name, pub), (x, share) in zip(guardians.items(), shares)
        },
        data_policy={"result_public_key": result_public_key},
        isolation_tier="sealed",
        machine_id=machine.id,
        is_challenge=True,
    )
    db.add(job)
    await db.flush()

    canary = CanaryJob(
        job_id=job.id,
        machine_id=machine.id,
        canary_token=token,
        canary_type=canary_type,
        status="active",
        details={"model": model_version},
    )
    db.add(canary)
    await db.flush()
    return job, canary


async def report_canary_leak(
    db: AsyncSession,
    *,
    canary_token: str,
    evidence_payload: dict[str, Any],
    actor_user_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Record a confirmed leak: ban the machine and record the slash of its whole bonded stake.

    Compensation: the canary's "customer" is the platform, so the whole slash is earmarked for the
    insurance pool. Real customer compensation for a leaked customer job goes through disputes.
    """
    ts = now or utcnow()
    canary = (await db.execute(select(CanaryJob).where(CanaryJob.canary_token == canary_token).with_for_update())).scalar_one_or_none()
    if canary is None:
        raise ValueError("unknown canary token")
    if canary.status == "leaked":
        return {
            "status": "already_slashed",
            "machine_id": str(canary.machine_id),
            "detected_at": canary.detected_at.isoformat() if canary.detected_at else None,
        }

    machine = (await db.execute(select(ProviderMachine).where(ProviderMachine.id == canary.machine_id).with_for_update())).scalar_one_or_none()
    if machine is None:
        raise ValueError("machine for this canary no longer exists")

    canary.status = "leaked"
    canary.detected_at = ts
    canary.details = {**(canary.details or {}), "leak_evidence": evidence_payload}

    slashed = (machine.stake_bonded or decimal.Decimal("0")).quantize(_CENT)
    machine.stake_bonded = decimal.Decimal("0.00")
    machine.is_banned = True
    machine.status = "deregistered"
    machine.availability = "offline"

    db.add(
        SlashEvent(
            provider_id=machine.provider_id,
            job_id=canary.job_id,
            amount_minor=int(slashed / _CENT),
            currency="COMPUTE",
            reason=f"CANARY_LEAK_CONFIRMED machine={machine.id} canary={canary.id}",
            actor_user_id=actor_user_id,
        )
    )
    await append_transparency_log_entry(
        db,
        "TAMPER_EVIDENCE",
        {
            "event": "CANARY_LEAK_SLASH",
            "machine_id": str(machine.id),
            "provider_id": str(machine.provider_id),
            "canary_fingerprint": token_fingerprint(canary_token),
            "slashed_amount": str(slashed),
            "recipient": "insurance_pool",
            "detected_at": ts.isoformat(),
        },
    )
    return {
        "status": "slashed",
        "machine_id": str(machine.id),
        "slashed_amount": str(slashed),
        "customer_compensation": "0.00",
        "penalty_to_insurance": str(slashed),
        "detected_at": ts.isoformat(),
    }
