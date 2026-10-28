"""Provider management endpoints (User JWT; owner-scoped)."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.machine_auth import generate_enrollment_token, hash_enrollment_token
from app.core.rate_limit import rate_limiter
from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.enums import Role
from app.models.provider import EnrollmentToken, MachineAttestation, Provider
from app.models.user import UserRole
from app.schemas.attestation import (
    AttestationChallengeResponse,
    AttestationReportOut,
    AttestationSubmitRequest,
    AttestationVerifyResponse,
)
from app.schemas.auth import CurrentUser
from app.schemas.provider import (
    EnrollmentTokenCreate,
    EnrollmentTokenCreatedOut,
    EnrollmentTokenOut,
    MachineDetailOut,
    MachineOut,
    MachineUpdate,
    ProviderOut,
    ProviderRegisterRequest,
)
from app.models.billing import LedgerEntry, LedgerTransaction, ProviderPayout
from app.schemas.billing import AmountIn, EarningsOut, LedgerEntryOut, PayoutOut
from app.services import ledger
from app.services.attestation_nonce import (
    consume_attestation_nonce,
    issue_attestation_nonce,
    require_fresh_attestation_nonce,
)
from app.services.attestation_verifier import AttestationVerifierError, verify_machine_evidence
from app.services.audit import record_audit
from app.services.provider import (
    get_owned_machine,
    get_provider_by_user,
    list_provider_machines,
    shape_machine,
    shape_machine_detail,
)
from sqlalchemy import func

router = APIRouter(prefix="/provider", tags=["provider"])

# Phase 5 billing routes live under the versioned /v1/provider prefix per contract.
billing_router = APIRouter(prefix="/v1/provider", tags=["provider-billing"])

_MACHINE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found"
)
_TOKEN_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment token not found"
)
_NOT_A_PROVIDER = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="Not registered as a provider"
)


async def _require_provider(db: AsyncSession, user: CurrentUser) -> Provider:
    provider = await get_provider_by_user(db, user.id)
    if provider is None:
        raise _NOT_A_PROVIDER
    return provider


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------
@router.post("/register", response_model=ProviderOut)
async def register_provider(
    body: ProviderRegisterRequest,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderOut:
    """Make the current user a provider (idempotent). Grants the ``provider`` role."""
    provider = await get_provider_by_user(db, current.id)
    created = False
    if provider is None:
        provider = Provider(
            user_id=current.id,
            display_name=body.display_name,
            contact_email=body.contact_email,
            status="active",
        )
        db.add(provider)
        await db.flush()
        created = True

    # Idempotently grant the provider role.
    existing_role = await db.execute(
        select(UserRole).where(
            UserRole.user_id == current.id, UserRole.role == Role.provider
        )
    )
    if existing_role.scalar_one_or_none() is None:
        db.add(UserRole(user_id=current.id, role=Role.provider))

    if created:
        await record_audit(
            db, action="provider.register", outcome="success", actor_user_id=current.id,
            ip=client_ip(request), resource_type="provider", resource_id=str(provider.id),
        )
    await db.flush()
    return ProviderOut(
        provider_id=provider.id,
        display_name=provider.display_name,
        contact_email=provider.contact_email,
        status=provider.status,
        created_at=provider.created_at,
    )


# ---------------------------------------------------------------------------
# Enrollment tokens
# ---------------------------------------------------------------------------
@router.post(
    "/enrollment-tokens",
    response_model=EnrollmentTokenCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def mint_enrollment_token(
    body: EnrollmentTokenCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EnrollmentTokenCreatedOut:
    provider = await _require_provider(db, current)
    if not rate_limiter.allow(
        f"enroll_token:{provider.id}",
        settings.ENROLLMENT_TOKEN_RATE_LIMIT,
        settings.ENROLLMENT_TOKEN_RATE_WINDOW_SECONDS,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests, please try again later",
        )

    raw = generate_enrollment_token()
    token = EnrollmentToken(
        provider_id=provider.id,
        token_hash=hash_enrollment_token(raw),
        label=body.label,
        expires_at=utcnow() + dt.timedelta(seconds=settings.ENROLLMENT_TOKEN_TTL_SECONDS),
    )
    db.add(token)
    await db.flush()
    await record_audit(
        db, action="provider.enrollment_token.mint", outcome="success",
        actor_user_id=current.id, ip=client_ip(request),
        resource_type="enrollment_token", resource_id=str(token.id),
    )
    await db.flush()
    return EnrollmentTokenCreatedOut(
        enrollment_token_id=token.id,
        enrollment_token=raw,  # shown exactly once
        expires_at=token.expires_at,
        label=token.label,
    )


@router.get("/enrollment-tokens", response_model=list[EnrollmentTokenOut])
async def list_enrollment_tokens(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EnrollmentTokenOut]:
    provider = await _require_provider(db, current)
    result = await db.execute(
        select(EnrollmentToken)
        .where(EnrollmentToken.provider_id == provider.id)
        .order_by(EnrollmentToken.created_at)
    )
    return [
        EnrollmentTokenOut(
            id=t.id,
            label=t.label,
            expires_at=t.expires_at,
            used_at=t.used_at,
            revoked_at=t.revoked_at,
            machine_id=t.machine_id,
            created_at=t.created_at,
        )
        for t in result.scalars().all()
    ]


@router.delete(
    "/enrollment-tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def revoke_enrollment_token(
    token_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    provider = await _require_provider(db, current)
    result = await db.execute(
        select(EnrollmentToken).where(
            EnrollmentToken.id == token_id,
            EnrollmentToken.provider_id == provider.id,
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        raise _TOKEN_NOT_FOUND
    if token.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Token already used"
        )
    if token.revoked_at is None:
        token.revoked_at = utcnow()
    await record_audit(
        db, action="provider.enrollment_token.revoke", outcome="success",
        actor_user_id=current.id, ip=client_ip(request),
        resource_type="enrollment_token", resource_id=str(token.id),
    )
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------
@router.get("/machines", response_model=list[MachineOut])
async def list_machines(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MachineOut]:
    provider = await _require_provider(db, current)
    machines = await list_provider_machines(db, provider.id)
    return [shape_machine(m) for m in machines]


@router.get("/machines/{machine_id}", response_model=MachineDetailOut)
async def get_machine(
    machine_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MachineDetailOut:
    provider = await _require_provider(db, current)
    machine = await get_owned_machine(db, provider.id, machine_id)
    if machine is None:
        raise _MACHINE_NOT_FOUND
    return shape_machine_detail(machine)


@router.patch("/machines/{machine_id}", response_model=MachineDetailOut)
async def update_machine(
    machine_id: uuid.UUID,
    body: MachineUpdate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MachineDetailOut:
    provider = await _require_provider(db, current)
    machine = await get_owned_machine(db, provider.id, machine_id)
    if machine is None:
        raise _MACHINE_NOT_FOUND

    if body.availability is not None:
        if body.availability not in {"online", "offline", "maintenance"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid availability",
            )
        machine.availability = body.availability
    if body.asking_price_per_hour is not None:
        machine.asking_price_per_hour = body.asking_price_per_hour
    if body.max_concurrent_jobs is not None:
        machine.max_concurrent_jobs = body.max_concurrent_jobs
    if body.region is not None:
        machine.region = body.region
    if body.supported_workloads is not None and machine.capability is not None:
        machine.capability.supported_workloads = body.supported_workloads

    await db.flush()
    await db.refresh(machine)
    return shape_machine_detail(machine)


@router.delete(
    "/machines/{machine_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def deregister_machine(
    machine_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    provider = await _require_provider(db, current)
    machine = await get_owned_machine(db, provider.id, machine_id)
    if machine is None:
        raise _MACHINE_NOT_FOUND
    # Deregister + revoke the machine credential (future heartbeats 401).
    machine.status = "deregistered"
    machine.availability = "offline"
    machine.signing_key = ""  # invalidate HMAC key
    await record_audit(
        db, action="provider.machine.deregister", outcome="success",
        actor_user_id=current.id, ip=client_ip(request),
        resource_type="machine", resource_id=str(machine.id),
    )
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Phase 5 — provider earnings + payouts (LIVE from the ledger)
# ---------------------------------------------------------------------------
_INSUFFICIENT_EARNINGS = HTTPException(
    status_code=status.HTTP_409_CONFLICT, detail="insufficient_earnings"
)


@billing_router.get("/earnings", response_model=EarningsOut)
async def provider_earnings(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EarningsOut:
    provider = await _require_provider(db, current)
    account = await ledger.get_provider_account(db, provider.id)
    lifetime = (
        await db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
                LedgerEntry.account_id == account.id, LedgerEntry.direction == "credit"
            )
        )
    ).scalar_one()
    recent_rows = (
        await db.execute(
            select(LedgerEntry, LedgerTransaction)
            .join(LedgerTransaction, LedgerEntry.transaction_id == LedgerTransaction.id)
            .where(LedgerEntry.account_id == account.id)
            .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
            .limit(20)
        )
    ).all()
    await db.commit()
    return EarningsOut(
        currency=account.currency,
        available=ledger.minor_to_str(account.balance_minor),
        lifetime=ledger.minor_to_str(int(lifetime)),
        recent=[
            LedgerEntryOut(
                entry_id=e.id,
                transaction_id=e.transaction_id,
                kind=t.kind,
                direction=e.direction,
                amount=ledger.minor_to_str(e.amount_minor),
                currency=e.currency,
                reference_type=t.reference_type,
                reference_id=t.reference_id,
                created_at=e.created_at,
            )
            for e, t in recent_rows
        ],
    )


def _shape_payout(p: ProviderPayout) -> PayoutOut:
    return PayoutOut(
        payout_id=p.id,
        amount=ledger.minor_to_str(p.amount_minor),
        currency=p.currency,
        status=p.status,
        created_at=p.created_at,
        settled_at=p.settled_at,
    )


@billing_router.post("/payouts", response_model=PayoutOut, status_code=status.HTTP_201_CREATED)
async def request_payout(
    body: AmountIn,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PayoutOut:
    provider = await _require_provider(db, current)
    account = await ledger.get_provider_account(db, provider.id)
    amount_minor = ledger.to_minor(body.amount)
    if amount_minor > account.balance_minor:
        raise _INSUFFICIENT_EARNINGS

    payout_pending = await ledger.get_platform_account(db, "payout_pending")
    payout = ProviderPayout(
        provider_id=provider.id, amount_minor=amount_minor, status="requested",
        actor_user_id=current.id,
    )
    db.add(payout)
    await db.flush()
    try:
        txn = await ledger.post_transaction(
            db,
            kind="payout_request",
            legs=[(account, "debit", amount_minor), (payout_pending, "credit", amount_minor)],
            idempotency_key=f"payout_request:{payout.id}",
            reference_type="payout",
            reference_id=str(payout.id),
            actor_user_id=current.id,
        )
    except ledger.InsufficientFunds:
        # Concurrent payouts could both pass the pre-check above; the ledger's
        # no-negative guard on provider_earnings refuses the overdraw (H4).
        raise _INSUFFICIENT_EARNINGS
    payout.request_txn_id = txn.id
    await record_audit(
        db, action="provider.payout.request", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="payout", resource_id=str(payout.id),
        reason=ledger.minor_to_str(amount_minor),
    )
    await db.commit()
    await db.refresh(payout)
    return _shape_payout(payout)


@billing_router.get("/payouts", response_model=list[PayoutOut])
async def list_payouts(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PayoutOut]:
    provider = await _require_provider(db, current)
    rows = (
        await db.execute(
            select(ProviderPayout)
            .where(ProviderPayout.provider_id == provider.id)
            .order_by(ProviderPayout.created_at.desc())
        )
    ).scalars().all()
    return [_shape_payout(p) for p in rows]


# ---------------------------------------------------------------------------
# Phase 8: Remote Hardware Attestation
# ---------------------------------------------------------------------------
@router.post(
    "/machines/{machine_id}/attestation/challenge",
    response_model=AttestationChallengeResponse,
)
async def create_attestation_challenge(
    machine_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttestationChallengeResponse:
    """Issue a fresh cryptographic challenge nonce to prevent replay attacks."""
    import secrets

    provider = await _require_provider(db, current)
    machine = await get_owned_machine(db, provider.id, machine_id)
    if machine is None:
        raise _MACHINE_NOT_FOUND

    nonce = secrets.token_hex(24)
    machine.last_health = issue_attestation_nonce(machine.last_health or {}, nonce, now=utcnow())
    await db.flush()
    return AttestationChallengeResponse(machine_id=machine.id, nonce=nonce)


@router.post(
    "/machines/{machine_id}/attestation/verify",
    response_model=AttestationVerifyResponse,
)
async def verify_machine_attestation(
    machine_id: uuid.UUID,
    body: AttestationSubmitRequest,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttestationVerifyResponse:
    """Cryptographically verify an attestation quote from a provider machine."""
    import hashlib
    import hmac
    import json

    provider = await _require_provider(db, current)
    machine = await get_owned_machine(db, provider.id, machine_id)
    if machine is None:
        raise _MACHINE_NOT_FOUND

    # 1. Nonce freshness check (same rules as the machine-HMAC route)
    health = dict(machine.last_health or {})
    require_fresh_attestation_nonce(health, body.nonce, now=utcnow())

    # 2. Quote verification (platform PCR hash + nonce)
    quote_hasher = hashlib.sha384()
    quote_hasher.update(body.nonce.encode("utf-8"))
    for k in sorted(body.pcr_measurements.keys()):
        quote_hasher.update(f"{k}:{body.pcr_measurements[k]}".encode("utf-8"))
    computed_quote = quote_hasher.hexdigest()
    if not hmac.compare_digest(body.platform_quote, computed_quote):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="platform_quote_tampered",
        )

    # 3. Signature verification against machine hardware signing key
    measurements_json = json.dumps(body.pcr_measurements, sort_keys=True)
    canonical_str = (
        f"{machine.id}\n"
        f"{body.isolation_tier}\n"
        f"{body.nonce}\n"
        f"{body.timestamp}\n"
        f"{measurements_json}\n"
        f"{body.platform_quote}"
    )
    expected_sig = hmac.new(
        machine.signing_key.encode("utf-8"),
        canonical_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(body.signature, expected_sig):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="signature_verification_failed",
        )

    now = utcnow()
    tier = body.isolation_tier

    # ADR-0005: the Sealed tiers are granted ONLY by the evidence check (trusted image, security
    # locks, stake, in-RAM keys). Confidential keeps its Phase-8 behaviour for capability, but a
    # key-release verdict is issued only when the evidence check passes. Failure here leaves the
    # nonce unconsumed and nothing recorded (the transaction rolls back).
    verdict = None
    leaf = None
    if tier in ("sealed", "sealed_pro") or (tier == "confidential" and settings.PRIVACY_SOFTWARE_ATTESTATION):
        try:
            verdict, leaf = await verify_machine_evidence(
                db,
                machine=machine,
                requested_tier=tier,
                nonce=body.nonce,
                pcr_measurements=body.pcr_measurements,
                ephemeral_public_key=body.ephemeral_public_key,
                receipt_signing_public_key=body.receipt_signing_public_key,
                security_state=body.security_state,
                now=now,
            )
        except AttestationVerifierError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc

    # Consume nonce
    machine.last_health = consume_attestation_nonce(health)

    # Record attestation in DB
    attestation = MachineAttestation(
        machine_id=machine.id,
        isolation_tier=tier,
        nonce=body.nonce,
        pcr_measurements=body.pcr_measurements,
        platform_quote=body.platform_quote,
        signature=body.signature,
        verification_status="verified",
        verified_at=now,
        details={
            "ip": client_ip(request),
            **({"verdict": verdict, "log_leaf_index": leaf.leaf_index} if verdict and leaf else {}),
        },
    )
    db.add(attestation)

    # Promote machine capability
    if machine.capability:
        existing_tiers = list(machine.capability.supported_isolation_tiers or ["container"])
        if tier not in existing_tiers:
            existing_tiers.append(tier)
        machine.capability.supported_isolation_tiers = existing_tiers
        machine.capability.verification_status = "VERIFIED"
    if machine.hardware:
        machine.hardware.verification_status = "VERIFIED"

    await record_audit(
        db,
        action="machine.attestation.verify",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="machine",
        resource_id=str(machine.id),
        reason=tier,
    )
    await db.flush()

    return AttestationVerifyResponse(
        verified=True,
        machine_id=machine.id,
        isolation_tier=body.isolation_tier,
        verification_status="VERIFIED",
        verified_at=now,
        attestation_id=attestation.id,
        details={"pcr_count": len(body.pcr_measurements)},
        verdict=verdict,
    )


@router.get(
    "/machines/{machine_id}/attestation/history",
    response_model=list[AttestationReportOut],
)
async def list_machine_attestations(
    machine_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AttestationReportOut]:
    """Retrieve the audit trail of remote attestations for a provider machine."""
    provider = await _require_provider(db, current)
    machine = await get_owned_machine(db, provider.id, machine_id)
    if machine is None:
        raise _MACHINE_NOT_FOUND

    rows = (
        await db.execute(
            select(MachineAttestation)
            .where(MachineAttestation.machine_id == machine.id)
            .order_by(MachineAttestation.created_at.desc())
        )
    ).scalars().all()
    return [
        AttestationReportOut(
            attestation_id=a.id,
            machine_id=a.machine_id,
            isolation_tier=a.isolation_tier,
            verification_status=a.verification_status,
            nonce=a.nonce,
            pcr_measurements=a.pcr_measurements or {},
            verified_at=a.verified_at,
            created_at=a.created_at,
        )
        for a in rows
    ]
