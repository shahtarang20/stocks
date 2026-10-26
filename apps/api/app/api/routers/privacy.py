"""API router for ADR-0005: Proof-of-Privacy.

Public (no account needed, by design: this is what makes receipts checkable by anyone):
  GET  /v1/privacy/verifier-key                 verifier Ed25519 key, guardian X25519 keys, threshold
  GET  /v1/privacy/transparency-log             log entries
  GET  /v1/privacy/transparency-log/proof/{i}   RFC 6962 inclusion proof
  GET  /v1/privacy/transparency-log/anchor      current root and size
  GET  /v1/privacy/trusted-images               approved OS images and reference PCRs
  POST /v1/privacy/receipts/verify              stateless receipt check (same as ccverify)

Authenticated:
  GET  /v1/privacy/receipts/{job_id}            job owner, the host that ran it, or an administrator
  POST /v1/privacy/freivalds/verify             any signed-in user
  POST /v1/privacy/trusted-images               administrator
  POST /v1/privacy/stake/bond                   administrator (records a vault deposit)
  POST /v1/privacy/canary/report-leak           security operator or administrator
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.deps import get_current_user, require_roles
from app.models.enums import Role
from app.models.jobs import Job
from app.models.privacy import PrivacyReceipt, TransparencyLogLeaf, TrustedImage
from app.models.provider import ProviderMachine
from app.schemas.auth import CurrentUser
from app.schemas.privacy import (
    CanaryLeakReportRequest,
    CanaryLeakReportResponse,
    FreivaldsVerifyRequest,
    FreivaldsVerifyResponse,
    InclusionProofOut,
    PrivacyReceiptOut,
    StakeBondRequest,
    StakeBondResponse,
    TransparencyLogLeafOut,
    TrustedImageCreate,
    TrustedImageOut,
    VerifierKeysOut,
)
from app.services.audit import record_audit
from app.services.canary import report_canary_leak
from app.services.freivalds import freivalds_verify
from app.services.privacy_keys import PrivacyConfigError, guardian_public_keys, guardian_threshold, verifier_public_key
from app.services.privacy_receipt import verify_privacy_receipt_offline
from app.services.transparency_log import (
    EMPTY_ROOT,
    append_transparency_log_entry,
    get_inclusion_proof_for_leaf,
    get_log_size,
)

router = APIRouter(prefix="/v1/privacy", tags=["privacy"])

_ADMIN = Role.administrator.value


@router.get("/verifier-key", response_model=VerifierKeysOut)
async def get_verifier_keys() -> VerifierKeysOut:
    """Everything needed to check receipts offline and to seal Data Key shares to guardians."""
    try:
        return VerifierKeysOut(
            verifier_public_key=verifier_public_key(),
            guardians=guardian_public_keys(),
            threshold=guardian_threshold(),
        )
    except PrivacyConfigError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Privacy Receipts
# ---------------------------------------------------------------------------

@router.get("/receipts/{job_id}", response_model=PrivacyReceiptOut)
async def get_job_privacy_receipt(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> PrivacyReceipt:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")
    if job.customer_id != current.id and _ADMIN not in current.roles:
        machine = None
        if job.machine_id:
            machine = (
                await db.execute(
                    select(ProviderMachine).options(selectinload(ProviderMachine.provider)).where(ProviderMachine.id == job.machine_id)
                )
            ).scalar_one_or_none()
        if machine is None or machine.provider is None or machine.provider.user_id != current.id:
            # Same answer as a missing job, so strangers cannot probe which job ids exist.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")

    receipt = (await db.execute(select(PrivacyReceipt).where(PrivacyReceipt.job_id == job_id))).scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="privacy_receipt_not_yet_generated")
    return receipt


@router.post("/receipts/verify")
async def verify_receipt_endpoint(receipt: dict[str, Any]) -> dict[str, Any]:
    """Stateless check of a receipt with this deployment's public verifier key."""
    return verify_privacy_receipt_offline(receipt)


# ---------------------------------------------------------------------------
# Merkle Transparency Log
# ---------------------------------------------------------------------------

@router.get("/transparency-log", response_model=list[TransparencyLogLeafOut])
async def list_transparency_log(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000_000),
    event_type: str | None = Query(default=None, max_length=32),
    db: AsyncSession = Depends(get_db),
) -> list[TransparencyLogLeaf]:
    query = select(TransparencyLogLeaf).order_by(TransparencyLogLeaf.leaf_index.asc())
    if event_type:
        query = query.where(TransparencyLogLeaf.event_type == event_type)
    res = await db.execute(query.offset(offset).limit(limit))
    return list(res.scalars().all())


@router.get("/transparency-log/proof/{leaf_index}", response_model=InclusionProofOut)
async def get_leaf_inclusion_proof(leaf_index: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    try:
        return await get_inclusion_proof_for_leaf(db, leaf_index)
    except (ValueError, IndexError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="leaf_not_found") from exc


@router.get("/transparency-log/anchor")
async def get_latest_transparency_anchor(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    latest = (
        await db.execute(select(TransparencyLogLeaf).order_by(desc(TransparencyLogLeaf.leaf_index)).limit(1))
    ).scalar_one_or_none()
    if latest is None:
        return {"merkle_root": EMPTY_ROOT, "total_leaves": 0, "latest_leaf_index": -1, "anchored_tx_hash": None, "anchored_block_number": None}
    return {
        "merkle_root": latest.merkle_root,
        "total_leaves": await get_log_size(db),
        "latest_leaf_index": latest.leaf_index,
        # On-chain anchoring is not built yet; these stay null until it is.
        "anchored_tx_hash": latest.anchored_tx_hash,
        "anchored_block_number": latest.anchored_block_number,
    }


# ---------------------------------------------------------------------------
# Trusted OS images
# ---------------------------------------------------------------------------

@router.get("/trusted-images", response_model=list[TrustedImageOut])
async def list_trusted_images(db: AsyncSession = Depends(get_db)) -> list[TrustedImage]:
    res = await db.execute(select(TrustedImage).order_by(TrustedImage.created_at.desc()))
    return list(res.scalars().all())


@router.post("/trusted-images", response_model=TrustedImageOut, status_code=status.HTTP_201_CREATED)
async def register_trusted_image(
    body: TrustedImageCreate,
    current: CurrentUser = Depends(require_roles(_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> TrustedImage:
    """Approve an OS image. Logged before it becomes usable, so a silent approval is impossible."""
    if (await db.execute(select(TrustedImage).where(TrustedImage.image_hash == body.image_hash))).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="image_hash_already_registered")
    await append_transparency_log_entry(
        db,
        "IMAGE_HASH",
        {"image_hash": body.image_hash, "version_tag": body.version_tag, "tier": body.tier, "expected_pcrs": body.expected_pcrs, "registered_by": str(current.id)},
    )
    img = TrustedImage(image_hash=body.image_hash, version_tag=body.version_tag, tier=body.tier, expected_pcrs=body.expected_pcrs, status="active")
    db.add(img)
    await record_audit(db, action="privacy.trusted_image.register", outcome="success", actor_user_id=current.id, resource_type="trusted_image", resource_id=body.image_hash)
    await db.commit()
    await db.refresh(img)
    return img


# ---------------------------------------------------------------------------
# Canary leak reporting
# ---------------------------------------------------------------------------

@router.post("/canary/report-leak", response_model=CanaryLeakReportResponse)
async def report_leak_endpoint(
    body: CanaryLeakReportRequest,
    current: CurrentUser = Depends(require_roles(Role.security_operator.value, _ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CanaryLeakReportResponse:
    """A security operator confirms a canary marker was found outside. Bans and records the slash."""
    try:
        outcome = await report_canary_leak(
            db,
            canary_token=body.canary_token,
            evidence_payload={"proof_description": body.proof_description, "evidence": body.evidence},
            actor_user_id=current.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await record_audit(db, action="privacy.canary.leak", outcome=outcome["status"], actor_user_id=current.id, resource_type="machine", resource_id=outcome.get("machine_id"))
    await db.commit()
    return CanaryLeakReportResponse(
        status=outcome["status"],
        machine_id=uuid.UUID(outcome["machine_id"]) if outcome.get("machine_id") else None,
        slashed_amount=outcome.get("slashed_amount"),
        customer_compensation=outcome.get("customer_compensation"),
        detected_at=dt.datetime.fromisoformat(outcome["detected_at"]) if outcome.get("detected_at") else None,
    )


# ---------------------------------------------------------------------------
# Freivalds integrity check
# ---------------------------------------------------------------------------

@router.post("/freivalds/verify", response_model=FreivaldsVerifyResponse)
async def freivalds_check_endpoint(
    body: FreivaldsVerifyRequest,
    current: CurrentUser = Depends(get_current_user),
) -> FreivaldsVerifyResponse:
    try:
        ok = freivalds_verify(body.matrix_a, body.matrix_b, body.matrix_c, rounds=body.rounds)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return FreivaldsVerifyResponse(verified=ok, rounds=body.rounds, error_bound=2.0 ** (-body.rounds))


# ---------------------------------------------------------------------------
# Stake bonding (administrator records a ComputeStakeVault deposit)
# ---------------------------------------------------------------------------

@router.post("/stake/bond", response_model=StakeBondResponse)
async def bond_stake_endpoint(
    body: StakeBondRequest,
    current: CurrentUser = Depends(require_roles(_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> StakeBondResponse:
    """Record stake that was deposited in the on-chain vault.

    Administrator-only: a host must not be able to grant itself stake. This does NOT grant any
    isolation tier; tiers come only from a successful attestation.
    """
    machine = (
        await db.execute(select(ProviderMachine).where(ProviderMachine.id == body.machine_id).with_for_update())
    ).scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="machine_not_found")
    if machine.is_banned:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="machine_is_banned")
    machine.stake_bonded = (machine.stake_bonded or 0) + body.amount
    await record_audit(
        db, action="privacy.stake.bond", outcome="success", actor_user_id=current.id,
        resource_type="machine", resource_id=str(machine.id), reason=f"{body.amount} COMPUTE tx={body.vault_tx_hash}",
    )
    await db.commit()
    await db.refresh(machine)
    eligible = ["open"]
    if machine.stake_bonded >= 1000:
        eligible.append("sealed")
    if machine.stake_bonded >= 5000 and machine.memory_encryption_active and machine.kyc_verified:
        eligible.append("sealed_pro")
    return StakeBondResponse(machine_id=machine.id, bonded_stake=machine.stake_bonded, eligible_tiers=eligible)
