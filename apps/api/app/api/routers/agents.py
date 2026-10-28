"""Agent endpoints — no user auth. Enroll (enrollment token) + heartbeat (HMAC)."""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.machine_auth import (
    build_canonical,
    generate_machine_credential,
    hash_enrollment_token,
    verify_signature,
)
from app.db.base import ensure_aware, utcnow
from app.db.session import get_db
from app.deps import client_ip
from app.models.jobs import Job, JobAttempt, JobLogMetadata
from app.models.provider import (
    EnrollmentToken,
    HardwareInventory,
    MachineAttestation,
    ProviderCapability,
    ProviderMachine,
)
import logging

logger = logging.getLogger(__name__)

from app.schemas.attestation import (
    AttestationChallengeResponse,
    AttestationSubmitRequest,
    AttestationVerifyResponse,
    TpmEnrolmentActivateRequest,
    TpmEnrolmentActivateResponse,
    TpmEnrolmentStartRequest,
    TpmEnrolmentStartResponse,
)
from app.services.attestation_nonce import (
    consume_attestation_nonce,
    issue_attestation_nonce,
    nonce_job_id,
    require_fresh_attestation_nonce,
)
from app.services.attestation_verifier import (
    AttestationVerifierError,
    PRIVACY_TIERS,
    TIER_RANK,
    verdict_is_current,
    verify_machine_evidence,
    verify_verdict_signature,
)
from app.services.key_guardians import (
    GuardianReleaseError,
    execute_guardian_release,
    prepare_guardian_release,
)
from app.services.privacy_crypto import is_sealed_envelope
from app.services.privacy_receipt import create_privacy_receipt_for_job
from app.schemas.jobs import (
    ArtifactUploadGrant,
    ClaimLimits,
    ClaimResponse,
    JobHeartbeatRequest,
    JobHeartbeatResponse,
    JobLogsAppendRequest,
    JobLogsAppendResponse,
    JobResultRequest,
    JobResultResponse,
)
from app.schemas.provider import (
    AgentEnrollRequest,
    AgentEnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
)
from app.services import billing
from app.services import job_fsm as fsm
from app.services.audit import record_audit
from app.services.leases import (
    close_running_attempt,
    recover_expired_leases,
    release_job_reservation,
)
from app.services import challenge as challenge_service
from app.services import reputation as reputation_svc
from app.services import verification as verification_service
from app.services.hooks import safe_hook
from app.services.scheduler import get_model_version
from app.services.storage import get_store, grant_expiry, make_upload_grant
from app.services.verification import verify_output
from app.services.webhooks import dispatch_event

router = APIRouter(prefix="/v1/agents", tags=["agents"])

_INVALID_TOKEN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid enrollment token"
)
_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
)
_JOB_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
)


async def authenticate_machine(request: Request, db: AsyncSession) -> tuple[ProviderMachine, bytes]:
    """Verify machine HMAC auth (same scheme as heartbeat) and return (machine, raw_body).

    Reuses build_canonical + verify_signature over ``machine_id\\n ts \\n sha256(body)``.
    Any failure is a flat 401 (no information leak).
    """
    raw_body = await request.body()
    machine_header = request.headers.get("X-ComputeCoin-Machine", "")
    ts_header = request.headers.get("X-ComputeCoin-Timestamp", "")
    sig_header = request.headers.get("X-ComputeCoin-Signature", "")

    try:
        machine_uuid = uuid.UUID(machine_header)
    except (ValueError, TypeError):
        raise _UNAUTHORIZED

    result = await db.execute(
        select(ProviderMachine).where(ProviderMachine.id == machine_uuid)
    )
    machine = result.scalar_one_or_none()
    if machine is None or machine.status == "deregistered" or not machine.signing_key:
        raise _UNAUTHORIZED

    try:
        ts = int(ts_header)
    except (ValueError, TypeError):
        raise _UNAUTHORIZED
    if abs(int(utcnow().timestamp()) - ts) > settings.MACHINE_HMAC_SKEW_SECONDS:
        raise _UNAUTHORIZED

    canonical = build_canonical(machine_header, ts_header, raw_body)
    if not verify_signature(machine.signing_key, canonical, sig_header):
        raise _UNAUTHORIZED
    return machine, raw_body


@router.post("/enroll", response_model=AgentEnrollResponse, status_code=status.HTTP_201_CREATED)
async def enroll(
    body: AgentEnrollRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AgentEnrollResponse:
    result = await db.execute(
        select(EnrollmentToken).where(
            EnrollmentToken.token_hash == hash_enrollment_token(body.enrollment_token)
        )
    )
    token = result.scalar_one_or_none()
    now = utcnow()
    if (
        token is None
        or token.used_at is not None
        or token.revoked_at is not None
        or ensure_aware(token.expires_at) <= now
    ):
        raise _INVALID_TOKEN

    report = body.capability_report
    first_gpu = report.gpus[0] if report.gpus else None

    # One machine per (provider, hostname). A deregistered machine releases its
    # hostname so a reinstalled host can enroll again; an ACTIVE duplicate is a
    # clear 409 and the token stays unused. This used to surface as a bare 500
    # from the unique constraint, and burned nothing but the provider's patience.
    same_host = (
        await db.execute(
            select(ProviderMachine).where(
                ProviderMachine.provider_id == token.provider_id,
                ProviderMachine.hostname == body.hostname,
            )
        )
    ).scalars().all()
    if any(m.status != "deregistered" for m in same_host):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="machine_hostname_already_enrolled",
        )
    for old in same_host:
        old.hostname = f"{old.hostname[:240]}~{str(old.id)[:8]}"
    if same_host:
        await db.flush()

    machine_secret, signing_key, secret_hash = generate_machine_credential()
    machine = ProviderMachine(
        provider_id=token.provider_id,
        hostname=body.hostname,
        agent_version=body.agent_version,
        status="registered",
        availability="offline",
        signing_key=signing_key,
        secret_hash=secret_hash,
    )
    db.add(machine)
    await db.flush()

    # Hardware snapshot — always DETECTED (never verified/benchmarked here).
    hardware = HardwareInventory(
        machine_id=machine.id,
        verification_status="detected",
        cpu_model=report.cpu_model,
        cpu_cores=report.cpu_cores,
        ram_mb=report.ram_mb,
        disk_gb=report.disk_gb,
        gpu_model=first_gpu.model if first_gpu else None,
        gpu_count=len(report.gpus),
        vram_mb=first_gpu.vram_mb if first_gpu else 0,
        cuda_version=first_gpu.cuda_version if first_gpu else None,
        raw=report.model_dump(),
    )
    db.add(hardware)
    db.add(
        ProviderCapability(
            machine_id=machine.id,
            supported_workloads=report.supported_workloads,
            # Privacy tiers are never taken from the host's own word: they are added only by a
            # successful attestation (ADR-0005). Sandboxing tiers stay self-reported, as before.
            supported_isolation_tiers=[
                t for t in (getattr(report, "isolation_tiers", None) or ["container"])
                if t not in ("sealed", "sealed_pro", "confidential")
            ] or ["container"],
            verification_status="detected",
        )
    )

    # Mark the token used + link the machine (single-use).
    token.used_at = now
    token.machine_id = machine.id

    await record_audit(
        db, action="agent.machine.enroll", outcome="success", actor_user_id=None,
        ip=client_ip(request), resource_type="machine", resource_id=str(machine.id),
    )
    await db.flush()

    return AgentEnrollResponse(
        machine_id=machine.id,
        machine_secret=machine_secret,  # shown once
        heartbeat_interval_seconds=settings.HEARTBEAT_INTERVAL_SECONDS,
        online_window_seconds=settings.ONLINE_WINDOW_SECONDS,
    )


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    request: Request,
    x_computecoin_machine: str = Header(default=""),
    x_computecoin_timestamp: str = Header(default=""),
    x_computecoin_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> HeartbeatResponse:
    raw_body = await request.body()

    # Resolve the machine. Any failure here is a flat 401 (no info leak).
    try:
        machine_uuid = uuid.UUID(x_computecoin_machine)
    except (ValueError, TypeError):
        raise _UNAUTHORIZED

    result = await db.execute(
        select(ProviderMachine).where(ProviderMachine.id == machine_uuid)
    )
    machine = result.scalar_one_or_none()
    if machine is None or machine.status == "deregistered" or not machine.signing_key:
        raise _UNAUTHORIZED

    # Timestamp skew / replay protection.
    try:
        ts = int(x_computecoin_timestamp)
    except (ValueError, TypeError):
        raise _UNAUTHORIZED
    now_unix = int(utcnow().timestamp())
    if abs(now_unix - ts) > settings.MACHINE_HMAC_SKEW_SECONDS:
        raise _UNAUTHORIZED

    # Recompute HMAC over the exact raw bytes and compare in constant time.
    canonical = build_canonical(x_computecoin_machine, x_computecoin_timestamp, raw_body)
    if not verify_signature(machine.signing_key, canonical, x_computecoin_signature):
        raise _UNAUTHORIZED

    # Signature valid — now parse the body.
    try:
        payload = HeartbeatRequest.model_validate(json.loads(raw_body or b"{}"))
    except (ValidationError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid heartbeat body"
        )

    if payload.availability in {"online", "maintenance", "offline"}:
        machine.availability = payload.availability
    machine.last_heartbeat_at = utcnow()
    # Keep attestation state (the pending challenge nonce) across heartbeats: they arrive every
    # 30 s and would otherwise erase a nonce between challenge and verify.
    kept = {k: v for k, v in (machine.last_health or {}).items() if k.startswith("latest_attestation_nonce")}
    machine.last_health = {**payload.health.model_dump(), **kept}
    await db.flush()

    return HeartbeatResponse(ok=True, directives={})


# ---------------------------------------------------------------------------
# Agent job protocol (machine HMAC auth) — Phase 4
# ---------------------------------------------------------------------------
def _parse_body(raw_body: bytes, schema):
    try:
        return schema.model_validate(json.loads(raw_body or b"{}"))
    except (ValidationError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_body"
        )


async def _owned_running_job(
    db: AsyncSession, machine: ProviderMachine, job_id: uuid.UUID, *, lock: bool = False
) -> Job:
    """Fetch a job that belongs to this machine (else 404 — isolation rule).

    ``lock=True`` takes a row lock so the terminal transition is serialized
    against a concurrent customer cancel. Without it, a cancel and a success
    result can both read RUNNING, and the hold is then BOTH released to the
    customer and captured to the provider for one authorization.
    """
    if lock:
        job = (
            await db.execute(select(Job).where(Job.id == job_id).with_for_update())
        ).scalar_one_or_none()
    else:
        job = await db.get(Job, job_id)
    if job is None or job.machine_id != machine.id:
        raise _JOB_NOT_FOUND
    return job


@router.post("/jobs/claim")
async def claim_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    machine, raw_body = await authenticate_machine(request, db)
    # Parse claim body (machine_id must match the authenticated machine).
    try:
        parsed = json.loads(raw_body or b"{}")
        claim_machine = parsed.get("machine_id")
        # Only an agent that says it holds a current attestation session gets private jobs.
        accept_private = parsed.get("accept_private") is True
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_body")
    if claim_machine is not None and str(claim_machine) != str(machine.id):
        # An agent may only claim for ITS own machine.
        raise _UNAUTHORIZED

    now = utcnow()
    # Recover any stale leases first so requeued jobs become claimable again.
    await recover_expired_leases(db, now)

    result = await db.execute(
        select(Job)
        .where(Job.machine_id == machine.id, Job.status == fsm.RESERVED)
        .order_by(Job.priority.desc(), Job.created_at)
        .with_for_update()
    )
    candidates = list(result.scalars().all())
    if not candidates:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # The machine's latest attestation verdict (fail closed: L1). Private jobs are claimable only
    # with a current, correctly signed verdict of a high enough tier; without one they are skipped
    # (not failed) so ordinary work queued for this machine is not blocked.
    res_att = await db.execute(
        select(MachineAttestation)
        .where(MachineAttestation.machine_id == machine.id)
        .order_by(MachineAttestation.created_at.desc())
    )
    attestation = res_att.scalars().first()
    verdict = (attestation.details or {}).get("verdict") if attestation else None
    if verdict and not (verdict_is_current(verdict, now) and verify_verdict_signature(verdict)):
        verdict = None

    def _private(j: Job) -> bool:
        return (getattr(j, "isolation_tier", None) or "open").lower() in PRIVACY_TIERS

    def _claimable(j: Job) -> bool:
        if not _private(j):
            return True
        return accept_private and verdict is not None and TIER_RANK.get(str(verdict.get("tier_reached", "")).lower(), -1) >= TIER_RANK.get(str(j.isolation_tier).lower(), 99)

    job = next((j for j in candidates if _claimable(j)), None)
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    is_private = _private(job)
    encrypted_input = None
    result_public_key = None
    job_input = job.input

    # Claim atomically: RESERVED -> RUNNING + lease. A second claim finds no
    # RESERVED job (claim-once).
    await fsm.transition(db, job, fsm.RUNNING, reason="claimed", actor="agent")
    lease_expires_at = now + dt.timedelta(seconds=settings.LEASE_TTL_SECONDS)
    job.lease_expires_at = lease_expires_at

    db.add(
        JobAttempt(
            job_id=job.id,
            attempt_number=job.attempts_made + 1,
            machine_id=machine.id,
            status="running",
        )
    )

    key_attestation_nonce = None
    if is_private:
        # R5 (one job, one key set): the claim hands out NO keys. It returns a challenge bound to
        # this job; the agent attests with fresh in-RAM keys for this job and then fetches the
        # shares from POST /v1/agents/jobs/{job_id}/keys with the resulting job-bound verdict.
        key_attestation_nonce = secrets.token_hex(24)
        machine.last_health = issue_attestation_nonce(
            machine.last_health or {}, key_attestation_nonce, now=now, job_id=str(job.id)
        )
        encrypted_input = job.encrypted_input
        result_public_key = (job.data_policy or {}).get("result_public_key")
        job_input = {}  # the platform never hands out plaintext input for a private job

    mv = await get_model_version(db, job.model)
    image_digest = mv.image_digest if mv else None

    grant_exp = grant_expiry()
    upload_token = make_upload_grant(str(job.id), grant_exp)

    await record_audit(
        db, action="job.claim", outcome="success", actor_user_id=None,
        ip=client_ip(request), resource_type="job", resource_id=str(job.id),
    )
    await db.flush()

    return ClaimResponse(
        job_id=job.id,
        workload_type=job.workload_type,
        model=job.model,
        input=job_input,
        execution_mode=job.execution_mode,
        isolation_tier=getattr(job, "isolation_tier", "container") or "container",
        limits=ClaimLimits(
            max_runtime_seconds=job.max_runtime_seconds,
            memory_mb=2048,
            cpus="1.0",
            gpu_count=job.gpu_count,
        ),
        image_digest=image_digest,
        artifact_upload=ArtifactUploadGrant(
            url=f"/v1/artifacts/upload/{upload_token}",
            token=upload_token,
            expires_at=grant_exp,
        ),
        lease_expires_at=lease_expires_at,
        encrypted_input=encrypted_input,
        result_public_key=result_public_key,
        key_attestation_required=key_attestation_nonce is not None,
        key_attestation_nonce=key_attestation_nonce,
    ).model_dump(mode="json")


async def _fail_claimed_job(db: AsyncSession, request: Request, job: Job, now: dt.datetime, code: str) -> None:
    """A claimed (RUNNING) job that cannot proceed: fail it, refund the hold, free the machine."""
    job.error_code = code
    job.error_message = "the job could not be started on this machine"
    await fsm.transition(db, job, fsm.FAILED, reason=code, actor="system")
    await close_running_attempt(db, job, now, outcome="failed", error_code=code)
    await billing.release_hold(db, job)
    await record_audit(
        db, action="job.fail", outcome="failure", actor_user_id=None,
        ip=client_ip(request), resource_type="job", resource_id=str(job.id), reason=code,
    )
    await _post_terminal_hooks(db, job)
    await release_job_reservation(db, job, now)
    await db.flush()


@router.post("/jobs/{job_id}/heartbeat", response_model=JobHeartbeatResponse)
async def job_heartbeat(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JobHeartbeatResponse:
    machine, raw_body = await authenticate_machine(request, db)
    body: JobHeartbeatRequest = _parse_body(raw_body, JobHeartbeatRequest)
    job = await _owned_running_job(db, machine, job_id)
    if job.status != fsm.RUNNING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job_not_running")

    now = utcnow()
    lease_expires_at = now + dt.timedelta(seconds=settings.LEASE_TTL_SECONDS)
    job.lease_expires_at = lease_expires_at
    if body.metrics is not None:
        merged = dict(job.metrics or {})
        merged.update(body.metrics)
        if body.progress is not None:
            merged["progress"] = body.progress
        job.metrics = merged
    elif body.progress is not None:
        merged = dict(job.metrics or {})
        merged["progress"] = body.progress
        job.metrics = merged
    await db.flush()
    return JobHeartbeatResponse(ok=True, lease_expires_at=lease_expires_at)


@router.post("/jobs/{job_id}/logs", response_model=JobLogsAppendResponse)
async def job_logs(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JobLogsAppendResponse:
    machine, raw_body = await authenticate_machine(request, db)
    body: JobLogsAppendRequest = _parse_body(raw_body, JobLogsAppendRequest)
    job = await _owned_running_job(db, machine, job_id)

    # ADR-0005 & WP3 Checkpoint 3.2: Log lines are dropped for private jobs
    if (getattr(job, "isolation_tier", None) or "open").lower() in ("sealed", "sealed_pro", "confidential"):
        return JobLogsAppendResponse(ok=True, stored_lines=0)

    result = await db.execute(
        select(JobLogMetadata).where(JobLogMetadata.job_id == job.id)
    )
    existing = result.scalars().all()
    next_seq = (max((m.seq for m in existing), default=-1)) + 1

    payload = ("\n".join(body.lines) + "\n").encode("utf-8") if body.lines else b""
    object_key = f"logs/{job.id}/{next_seq}.log"
    get_store().put(object_key, payload, "text/plain")
    db.add(
        JobLogMetadata(
            job_id=job.id,
            seq=next_seq,
            object_key=object_key,
            line_count=len(body.lines),
            size_bytes=len(payload),
        )
    )
    await db.flush()
    return JobLogsAppendResponse(ok=True, stored_lines=len(body.lines))


async def _post_terminal_hooks(
    db: AsyncSession, job: Job, *, invoice=None
) -> None:
    """Observational side effects for a job that just reached a terminal state.

    Reputation folding and webhook fan-out are reporting, not bookkeeping: the
    money and the FSM are already correct when we get here. Each runs in its own
    savepoint so a bad sink URL or a reputation write error can never undo the
    settlement or re-enter the result path (which would risk a double charge).
    Shadow and challenge jobs are platform-internal and are not announced.
    """
    if verification_service.is_unbilled_job(job):
        return

    async with safe_hook(db, "reputation.record_job_outcome"):
        await reputation_svc.record_job_outcome(db, job)

    event = "job.completed" if job.status == fsm.COMPLETED else "job.failed"
    async with safe_hook(db, f"webhooks.{event}"):
        await dispatch_event(
            db,
            customer_id=job.customer_id,
            event_type=event,
            payload={
                "job_id": str(job.id),
                "status": job.status,
                "execution_mode": job.execution_mode,
                "error_code": job.error_code,
            },
            source_id=str(job.id),
        )
    if invoice is not None:
        async with safe_hook(db, "webhooks.invoice.paid"):
            await dispatch_event(
                db,
                customer_id=job.customer_id,
                event_type="invoice.paid",
                payload={
                    "invoice_id": str(invoice.id),
                    "job_id": str(job.id),
                    "status": invoice.status,
                },
                source_id=str(invoice.id),
            )


@router.post("/jobs/{job_id}/result", response_model=JobResultResponse)
async def job_result(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JobResultResponse:
    machine, raw_body = await authenticate_machine(request, db)
    body: JobResultRequest = _parse_body(raw_body, JobResultRequest)
    # Locked: the status read below decides whether money is captured, so it must
    # not race a concurrent cancel (which releases the same hold).
    job = await _owned_running_job(db, machine, job_id, lock=True)

    # Idempotent: a repeated result for an already-terminal job returns the same status.
    if job.status in fsm.TERMINAL_STATES:
        return JobResultResponse(job_id=job.id, status=job.status)

    if job.status != fsm.RUNNING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job_not_running")

    now = utcnow()
    is_private = (getattr(job, "isolation_tier", None) or "open").lower() in PRIVACY_TIERS
    # Private jobs: the platform never stores plaintext output, even if an agent sends it.
    job.output = None if is_private else body.output
    if body.metrics is not None:
        merged = dict(job.metrics or {})
        merged.update(body.metrics)
        job.metrics = merged

    if body.status == "success":
        await fsm.transition(db, job, fsm.VERIFYING, reason="result_received", actor="agent")

        if is_private:
            commitment_ok = body.output_commitment is None or (
                len(body.output_commitment) == 64 and all(c in "0123456789abcdef" for c in body.output_commitment)
            )
            if not is_sealed_envelope(body.encrypted_output) or not commitment_ok:
                job.error_code = "encrypted_output_invalid"
                job.error_message = "private job result needs a sealed encrypted_output and a 64-hex output_commitment"
                job.verification = {"valid": False, "reason": job.error_code}
                await fsm.transition(db, job, fsm.FAILED, reason="encrypted_output_invalid", actor="system")
                await close_running_attempt(db, job, now, outcome="failed", error_code=job.error_code)
                await billing.release_hold(db, job)
                await record_audit(
                    db, action="job.fail", outcome="failure", actor_user_id=None,
                    ip=client_ip(request), resource_type="job", resource_id=str(job.id),
                    reason=job.error_code,
                )
                await _post_terminal_hooks(db, job)
                await release_job_reservation(db, job, now)
                await db.flush()
                return JobResultResponse(job_id=job.id, status=job.status)

            job.encrypted_output = body.encrypted_output
            if body.output_commitment:
                job.output_commitment = body.output_commitment

            # Link the receipt to the verdict the shares were released under at claim time,
            # not whatever the machine attested most recently (a re-attestation mid-job
            # carries different host keys and a different log leaf).
            claimed_verdict_id = next(
                (r.get("verdict_id") for r in (job.guardian_releases or []) if r.get("verdict_id")),
                None,
            )
            res_att = await db.execute(
                select(MachineAttestation)
                .where(MachineAttestation.machine_id == machine.id)
                .order_by(MachineAttestation.created_at.desc())
            )
            verdict: dict = {}
            verdict_leaf_index = None
            for attestation in res_att.scalars():
                details = attestation.details or {}
                candidate = details.get("verdict") or {}
                if candidate and (claimed_verdict_id is None or candidate.get("verdict_id") == claimed_verdict_id):
                    verdict = candidate
                    verdict_leaf_index = details.get("log_leaf_index")
                    break

            try:
                if verdict_leaf_index is None:
                    raise ValueError("attestation verdict for this job has no transparency log leaf")
                receipt = await create_privacy_receipt_for_job(
                    db,
                    job=job,
                    machine=machine,
                    attestation_verdict=verdict,
                    verdict_leaf_index=verdict_leaf_index,
                    guardian_releases=job.guardian_releases or [],
                    wipe_receipt=body.wipe_receipt,
                )
                job.privacy_receipt_id = receipt.id
                job.verification = {
                    "valid": True,
                    "method": "proof_of_privacy",
                    "receipt_id": str(receipt.id),
                }
            except Exception as exc:
                logger.error("Privacy receipt creation failed for job %s: %s", job.id, exc)
                job.error_code = "privacy_receipt_generation_failed"
                job.error_message = f"privacy receipt failed: {exc}"
                job.verification = {"valid": False, "reason": job.error_code}
                await fsm.transition(db, job, fsm.FAILED, reason="privacy_receipt_failed", actor="system")
                await close_running_attempt(db, job, now, outcome="failed", error_code=job.error_code)
                await billing.release_hold(db, job)
                await record_audit(
                    db, action="job.fail", outcome="failure", actor_user_id=None,
                    ip=client_ip(request), resource_type="job", resource_id=str(job.id),
                    reason=job.error_code,
                )
                await _post_terminal_hooks(db, job)
                await release_job_reservation(db, job, now)
                await db.flush()
                return JobResultResponse(job_id=job.id, status=job.status)

            await fsm.transition(db, job, fsm.COMPLETED, reason="verified", actor="system")
            await close_running_attempt(db, job, now, outcome="completed")
            invoice = None
            if not verification_service.is_unbilled_job(job):
                invoice = await billing.settle_job(db, job)
                async with safe_hook(db, "verification.shadow_sample"):
                    await verification_service.maybe_create_shadow_job(db, job)
            elif job.shadow_of_job_id is not None:
                async with safe_hook(db, "verification.resolve_redundancy"):
                    await verification_service.resolve_redundancy(db, job, body.output or {})
            await record_audit(
                db, action="job.complete", outcome="success", actor_user_id=None,
                ip=client_ip(request), resource_type="job", resource_id=str(job.id),
            )
            await _post_terminal_hooks(db, job, invoice=invoice)
        else:
            # Audit hooks run BEFORE job.verification is overwritten: the challenge
            # link lives there. Both are advisory evidence — a failure must not cost
            # the provider a settled job, so each runs in its own savepoint.
            async with safe_hook(db, "challenge.evaluate"):
                await challenge_service.evaluate_challenge_job(db, job, body.output or {})
            async with safe_hook(db, "verification.model_identity"):
                await verification_service.check_model_identity(
                    db, job, getattr(body, "model_weight_hash", None)
                )
            verification = verify_output(job.workload_type, job.input, body.output or {})
            job.verification = verification
            if verification["valid"]:
                await fsm.transition(db, job, fsm.COMPLETED, reason="verified", actor="system")
                await close_running_attempt(db, job, now, outcome="completed")
                invoice = None
                if not verification_service.is_unbilled_job(job):
                    invoice = await billing.settle_job(db, job)
                    async with safe_hook(db, "verification.shadow_sample"):
                        await verification_service.maybe_create_shadow_job(db, job)
                elif job.shadow_of_job_id is not None:
                    async with safe_hook(db, "verification.resolve_redundancy"):
                        await verification_service.resolve_redundancy(db, job, body.output or {})
                await record_audit(
                    db, action="job.complete", outcome="success", actor_user_id=None,
                    ip=client_ip(request), resource_type="job", resource_id=str(job.id),
                )
                await _post_terminal_hooks(db, job, invoice=invoice)
            else:
                job.error_code = "output_verification_failed"
                job.error_message = f"verification failed: {verification.get('reason')}"
                await fsm.transition(db, job, fsm.FAILED, reason="verification_failed", actor="system")
                await close_running_attempt(
                    db, job, now, outcome="failed", error_code=job.error_code
                )
                # A failed job is never charged — return the full hold.
                await billing.release_hold(db, job)
                await record_audit(
                    db, action="job.fail", outcome="failure", actor_user_id=None,
                    ip=client_ip(request), resource_type="job", resource_id=str(job.id),
                    reason=job.error_code,
                )
                await _post_terminal_hooks(db, job)
    else:  # "failed"
        err = body.error or {}
        job.error_code = (err.get("code") or "agent_reported_failure")[:64]
        job.error_message = (err.get("message") or "agent reported failure")[:1024]
        job.verification = {"valid": False, "reason": "agent_reported_failure"}
        await fsm.transition(db, job, fsm.FAILED, reason="agent_failed", actor="agent")
        await close_running_attempt(db, job, now, outcome="failed", error_code=job.error_code)
        # A failed job is never charged — return the full hold.
        await billing.release_hold(db, job)
        await record_audit(
            db, action="job.fail", outcome="failure", actor_user_id=None,
            ip=client_ip(request), resource_type="job", resource_id=str(job.id),
            reason=job.error_code,
        )
        await _post_terminal_hooks(db, job)

    # The reservation is ALWAYS released on a terminal transition (no capacity leak).
    await release_job_reservation(db, job, now)
    await db.flush()
    return JobResultResponse(job_id=job.id, status=job.status)


@router.post("/jobs/{job_id}/keys")
async def job_keys(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Release a private job's Data Key shares to the host that attested FOR THIS JOB (WP-B).

    Body: ``{"verdict_id": "..."}`` naming the job-bound verdict from /v1/agents/attestation/verify.
    Released once per job. Order (R6): log KEY_RELEASE entries -> COMMIT -> release. Any failure
    fails the job cleanly (hold refunded, machine freed).
    """
    machine, raw_body = await authenticate_machine(request, db)
    try:
        verdict_id = str(json.loads(raw_body or b"{}").get("verdict_id") or "")
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_body")
    job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None or job.machine_id != machine.id or job.status != fsm.RUNNING:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")
    if (job.isolation_tier or "").lower() not in PRIVACY_TIERS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_a_private_job")

    verdict = None
    rows = await db.execute(
        select(MachineAttestation)
        .where(MachineAttestation.machine_id == machine.id)
        .order_by(MachineAttestation.created_at.desc())
        .limit(20)
    )
    for att in rows.scalars():
        candidate = (att.details or {}).get("verdict") or {}
        if verdict_id and candidate.get("verdict_id") == verdict_id:
            verdict = candidate
            break
    if verdict is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="verdict_not_found")

    now = utcnow()
    try:
        plan = await prepare_guardian_release(db, job=job, machine=machine, attestation_verdict=verdict, now=now)
        # Claimed BEFORE the commit releases the row lock: a second concurrent request now sees
        # the job as already releasing and is refused (keys_already_released).
        job.guardian_releases = [{"status": "releasing", "verdict_id": verdict["verdict_id"]}]
        await db.commit()
        released = await execute_guardian_release(db, job=job, machine=machine, attestation_verdict=verdict, plan=plan)
    except GuardianReleaseError as exc:
        if exc.code in ("verdict_not_for_this_job", "keys_already_released", "machine_mismatch"):
            # A wrong or replayed request from the host: refuse it, but do not punish the job.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
        await _fail_claimed_job(db, request, job, utcnow(), f"key_release_failed:{exc.code}"[:64])
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": "key_release_failed", "reason": exc.code})
    job.guardian_releases = released
    await db.flush()
    return {
        "job_id": str(job.id),
        "verdict_id": verdict["verdict_id"],
        "wrapped_shares": [
            {"guardian": r["guardian"], "share_index": r["share_index"], "wrapped_share": r["wrapped_share"]} for r in released
        ],
    }


# ---------------------------------------------------------------------------
# Machine-signed Attestation (ADR-0005, WP2, Locked Decision L3)
# ---------------------------------------------------------------------------

@router.post(
    "/attestation/challenge",
    response_model=AttestationChallengeResponse,
)
async def agent_attestation_challenge(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AttestationChallengeResponse:
    """Issue a fresh challenge nonce to an enrolled machine via machine HMAC auth."""
    import secrets
    machine, _ = await authenticate_machine(request, db)

    nonce = secrets.token_hex(24)
    machine.last_health = issue_attestation_nonce(machine.last_health or {}, nonce, now=utcnow())
    await db.flush()
    return AttestationChallengeResponse(machine_id=machine.id, nonce=nonce)


@router.post(
    "/attestation/verify",
    response_model=AttestationVerifyResponse,
)
async def agent_attestation_verify(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AttestationVerifyResponse:
    """Cryptographically verify an attestation quote from a machine via machine HMAC auth."""
    import hashlib
    import hmac

    machine, raw_body = await authenticate_machine(request, db)
    try:
        data = json.loads(raw_body)
        body = AttestationSubmitRequest(**data)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # 1. Nonce freshness check
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

    # A challenge issued at claim time is bound to one private job (WP-B). The verdict it yields
    # names that job, and only such a verdict can release that job's keys.
    bound_job_id = nonce_job_id(health)
    if bound_job_id is not None:
        bound_job = (
            await db.execute(select(Job).where(Job.id == uuid.UUID(bound_job_id)).with_for_update())
        ).scalar_one_or_none()
        if (
            bound_job is None
            or bound_job.machine_id != machine.id
            or bound_job.status != fsm.RUNNING
            or (bound_job.isolation_tier or "").lower() not in PRIVACY_TIERS
            or TIER_RANK.get(tier, -1) < TIER_RANK.get((bound_job.isolation_tier or "").lower(), 99)
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job_not_attestable")

    verdict = None
    leaf = None
    if bound_job_id is not None or tier in ("sealed", "sealed_pro") or (
        tier == "confidential" and settings.PRIVACY_SOFTWARE_ATTESTATION
    ):
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
                job_id=bound_job_id,
                hardware_evidence=body.hardware_evidence,
                hardware_flavor=body.hardware_flavor,
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
    cap_res = await db.execute(
        select(ProviderCapability).where(ProviderCapability.machine_id == machine.id)
    )
    cap = cap_res.scalar_one_or_none()
    if cap:
        existing_tiers = list(cap.supported_isolation_tiers or ["container"])
        if tier not in existing_tiers:
            existing_tiers.append(tier)
            cap.supported_isolation_tiers = existing_tiers
        # Same promotion as the provider-auth route: the scheduler only routes private
        # tiers to capabilities marked VERIFIED.
        cap.verification_status = "VERIFIED"

    await db.flush()
    return AttestationVerifyResponse(
        verified=True,
        machine_id=machine.id,
        isolation_tier=tier,
        verification_status="verified",
        verified_at=now,
        attestation_id=attestation.id,
        details=attestation.details,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# TPM attestation-key enrolment (WP-F): bind the AK to the EK by credential activation
# ---------------------------------------------------------------------------

@router.post("/tpm/enrolments", response_model=TpmEnrolmentStartResponse)
async def tpm_enrolment_start(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    """Check the EK chain and the AK, then return a TPM2_MakeCredential challenge for them."""
    from app.services.tpm_enrolment import TpmEnrolmentError, start_enrolment

    machine, raw_body = await authenticate_machine(request, db)
    body: TpmEnrolmentStartRequest = _parse_body(raw_body, TpmEnrolmentStartRequest)
    try:
        enrolment, credential_blob, encrypted_secret = await start_enrolment(
            db, machine=machine, ek_cert_pem=body.ek_cert, ek_intermediates=body.ek_intermediates, ak_public_hex=body.ak_public
        )
    except TpmEnrolmentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    return TpmEnrolmentStartResponse(
        enrolment_id=enrolment.id,
        ak_name=enrolment.ak_name,
        credential_blob=credential_blob.hex(),
        encrypted_secret=encrypted_secret.hex(),
        expires_at=enrolment.expires_at,
    )


@router.post("/tpm/enrolments/{enrolment_id}/activate", response_model=TpmEnrolmentActivateResponse)
async def tpm_enrolment_activate(enrolment_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    """The host returns the secret TPM2_ActivateCredential recovered; the AK becomes active."""
    from app.services.tpm_enrolment import TpmEnrolmentError, activate_enrolment

    machine, raw_body = await authenticate_machine(request, db)
    body: TpmEnrolmentActivateRequest = _parse_body(raw_body, TpmEnrolmentActivateRequest)
    try:
        enrolment = await activate_enrolment(db, machine=machine, enrolment_id=enrolment_id, secret_hex=body.secret)
    except TpmEnrolmentError as exc:
        # Returned, not raised: the failed attempt (and an expiry or lock-out) must be committed.
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": exc.code})
    await record_audit(
        db, action="machine.tpm_enrolled", outcome="success", actor_user_id=None,
        ip=client_ip(request), resource_type="machine", resource_id=str(machine.id),
    )
    return TpmEnrolmentActivateResponse(enrolment_id=enrolment.id, ak_name=enrolment.ak_name, status=enrolment.status)

