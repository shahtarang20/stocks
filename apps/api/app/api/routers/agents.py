"""Agent endpoints — no user auth. Enroll (enrollment token) + heartbeat (HMAC)."""

from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
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
    ProviderCapability,
    ProviderMachine,
)
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
from app.services.scheduler import get_model_version
from app.services.storage import get_store, grant_expiry, make_upload_grant
from app.services.verification import verify_output

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
            supported_isolation_tiers=getattr(report, "isolation_tiers", None) or ["container"],
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
    machine.last_health = payload.health.model_dump()
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
    db: AsyncSession, machine: ProviderMachine, job_id: uuid.UUID
) -> Job:
    """Fetch a job that belongs to this machine (else 404 — isolation rule)."""
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
    job = result.scalars().first()
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        input=job.input,
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
    ).model_dump(mode="json")


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


@router.post("/jobs/{job_id}/result", response_model=JobResultResponse)
async def job_result(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JobResultResponse:
    machine, raw_body = await authenticate_machine(request, db)
    body: JobResultRequest = _parse_body(raw_body, JobResultRequest)
    job = await _owned_running_job(db, machine, job_id)

    # Idempotent: a repeated result for an already-terminal job returns the same status.
    if job.status in fsm.TERMINAL_STATES:
        return JobResultResponse(job_id=job.id, status=job.status)

    if job.status != fsm.RUNNING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job_not_running")

    now = utcnow()
    job.output = body.output
    if body.metrics is not None:
        merged = dict(job.metrics or {})
        merged.update(body.metrics)
        job.metrics = merged

    if body.status == "success":
        await fsm.transition(db, job, fsm.VERIFYING, reason="result_received", actor="agent")
        verification = verify_output(job.workload_type, job.input, body.output or {})
        job.verification = verification
        if verification["valid"]:
            await fsm.transition(db, job, fsm.COMPLETED, reason="verified", actor="system")
            await close_running_attempt(db, job, now, outcome="completed")
            # Settle: capture the metered actual cost + release the remainder.
            await billing.settle_job(db, job)
            await record_audit(
                db, action="job.complete", outcome="success", actor_user_id=None,
                ip=client_ip(request), resource_type="job", resource_id=str(job.id),
            )
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

    # The reservation is ALWAYS released on a terminal transition (no capacity leak).
    await release_job_reservation(db, job, now)
    await db.flush()
    return JobResultResponse(job_id=job.id, status=job.status)
