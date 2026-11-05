"""Services / endpoints runtime (U11, docs/architecture/U11-services-design.md).

Replicas are ordinary Job Spec v2 (Kit) jobs placed by the existing scheduler and billed by the existing job
billing (read-only reuse). This module adds: service creation, the pull-based request queue, the autoscaling
controller, failover and a read-only billing reconciliation. It never touches ledger, pricing or billing code.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import decimal
import logging
import re
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.billing import Invoice, UsageRecord
from app.models.jobs import Job
from app.models.services import (
    KIND_GENERIC, KIND_INFERENCE, REPLICA_DEAD, REPLICA_DRAINING, REPLICA_HEALTHY, REPLICA_LIVE, REPLICA_STARTING,
    REPLICA_STOPPED, REQ_ASSIGNED, REQ_DONE, REQ_EXPIRED, REQ_FAILED, REQ_OPEN, REQ_QUEUED, SERVICE_ACTIVE,
    SERVICE_DELETED, Service, ServiceReplica, ServiceRequest, ServiceRequestCharge,
)
from app.services import job_fsm as fsm

logger = logging.getLogger(__name__)

PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")
MAX_ATTEMPTS = 2  # first hand-off plus at most ONE retry
MAX_QUEUE_PER_SERVICE = 200
ROLLOVER_LEAD_SECONDS = 120
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$")
_CT_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,64}( *; *[A-Za-z0-9=._\"+ -]{1,64})?$")


class ServiceError(Exception):
    def __init__(self, code: str, http_status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _dec(v: Any) -> decimal.Decimal:
    return decimal.Decimal(str(v))


def valid_path(path: str) -> bool:
    return bool(_PATH_RE.fullmatch(path or "")) and ".." not in path and "//" not in path  # fullmatch: ``$`` accepts a trailing newline


def valid_content_type(ct: str) -> bool:
    return bool(_CT_RE.fullmatch(ct or ""))


def _aware(v: dt.datetime | None) -> dt.datetime | None:
    return ensure_aware(v) if v is not None else None


# --------------------------------------------------------------------------------------------
# Service creation
# --------------------------------------------------------------------------------------------
async def create_service(
    db: AsyncSession, *, customer_id: uuid.UUID, project_id: uuid.UUID | None, name: str, kind: str, spec_raw: Any,
    isolation_tier: str, required_gpu_runtime: str, min_replicas: int, max_replicas: int, scale_to_zero: bool,
    port: int, health_path: str, request_timeout_seconds: int | None, max_total_cost: decimal.Decimal | None,
    max_concurrency: int = 1,
) -> Service:
    """Validate and persist a service (no replica yet; the controller starts them). Caller commits."""
    from app.api.routers.jobs_v2 import build_v2_plan
    from app.services.kits import SERVICE_MODES, KitError, validate_job_spec_v2

    if not settings.SERVICES_ENABLED:
        raise ServiceError("services_disabled", 503)
    if isolation_tier not in ("container", "open"):
        raise ServiceError("privacy_tier_not_supported_for_services")  # U18 later
    if kind not in (KIND_GENERIC, KIND_INFERENCE):
        raise ServiceError("invalid_kind")
    if not valid_path(health_path):
        raise ServiceError("invalid_health_path")
    if not (1 <= port <= 65535):
        raise ServiceError("invalid_port")
    limit = max(1, int(settings.SERVICE_MAX_REPLICAS_LIMIT))
    if max_replicas < 1 or max_replicas > limit or min_replicas < 0 or min_replicas > max_replicas:
        raise ServiceError("invalid_replica_bounds")
    if min_replicas == 0 and not scale_to_zero:
        raise ServiceError("min_replicas_zero_needs_scale_to_zero")
    if not (1 <= int(max_concurrency) <= min(64, int(settings.SERVICE_MAX_CONCURRENCY_LIMIT))):
        raise ServiceError("invalid_max_concurrency")
    timeout = int(request_timeout_seconds or settings.SERVICE_REQUEST_TIMEOUT_SECONDS)
    if not (1 <= timeout <= 300):
        raise ServiceError("invalid_request_timeout")
    cap = max_total_cost if max_total_cost is not None else _dec(settings.SERVICE_DEFAULT_SPEND_CAP)
    if cap <= 0 or cap > _dec(settings.SERVICE_MAX_SPEND_CAP):
        raise ServiceError("invalid_spend_cap")
    live = await db.scalar(
        select(func.count()).select_from(Service).where(Service.customer_id == customer_id, Service.status != SERVICE_DELETED))
    if int(live or 0) >= int(settings.SERVICE_MAX_PER_CUSTOMER):
        raise ServiceError("too_many_services", 429)
    try:
        validated = await validate_job_spec_v2(db, spec_raw, allowed_kit_modes=SERVICE_MODES)
        await build_v2_plan(db, customer_id, validated.spec, validated.kit)
    except KitError as exc:
        field_codes = [e.get("code") for e in exc.errors if e.get("field") == "mode"]
        raise ServiceError(field_codes[0] if field_codes else exc.code, exc.http_status)
    kit = validated.kit
    block = (kit.manifest or {}).get("service")
    if (kit.manifest or {}).get("mode") != "service" or not isinstance(block, dict):
        raise ServiceError("mode_not_service")
    # The kit declares where its server listens: the request may not point the platform at another port or path.
    if int(block.get("port", -1)) != port:
        raise ServiceError("port_differs_from_kit_service_block")
    if block.get("health_path") != health_path:
        raise ServiceError("health_path_differs_from_kit_service_block")
    if block.get("max_concurrency") is not None and int(max_concurrency) > int(block["max_concurrency"]):
        raise ServiceError("max_concurrency_above_kit_limit")
    svc = Service(
        customer_id=customer_id, project_id=project_id, name=name, kind=kind, status=SERVICE_ACTIVE,
        spec=validated.spec.model_dump(mode="json"), kit_ref=f"{kit.slug}@{kit.version}",
        isolation_tier=isolation_tier, required_gpu_runtime=required_gpu_runtime.lower(),
        min_replicas=min_replicas, max_replicas=max_replicas, scale_to_zero=scale_to_zero, port=port,
        health_path=health_path, request_timeout_seconds=timeout, max_total_cost=cap, desired_replicas=min_replicas,
        max_concurrency=int(max_concurrency),
    )
    db.add(svc)
    await db.flush()
    return svc


# --------------------------------------------------------------------------------------------
# Replicas (ordinary jobs)
# --------------------------------------------------------------------------------------------
async def _machines_in_use(db: AsyncSession, service_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (await db.execute(
        select(ServiceReplica.machine_id).where(
            ServiceReplica.service_id == service_id, ServiceReplica.status.in_(REPLICA_LIVE),
            ServiceReplica.machine_id.is_not(None)))).scalars().all()
    out = {m for m in rows if m is not None}
    # A host that just timed out starting a replica is avoided so the replacement lands elsewhere.
    since = utcnow() - dt.timedelta(seconds=max(3600, 2 * int(settings.SERVICE_REPLICA_START_TIMEOUT_SECONDS)))
    bad = (await db.execute(select(ServiceReplica.machine_id).where(
        ServiceReplica.service_id == service_id, ServiceReplica.end_reason == "start_timeout",
        ServiceReplica.ended_at >= since, ServiceReplica.machine_id.is_not(None)))).scalars().all()
    return out | {m for m in bad if m is not None}


async def committed_cost(db: AsyncSession, service: Service) -> decimal.Decimal:
    """Final cost of ended replica jobs plus the authorised amount of live ones (what the cap is checked against)."""
    total = decimal.Decimal(0)
    replicas = (await db.execute(select(ServiceReplica).where(ServiceReplica.service_id == service.id))).scalars().all()
    for r in replicas:
        job = await db.get(Job, r.job_id)
        if job is not None and job.final_cost is not None:
            total += _dec(job.final_cost)
        elif r.status in REPLICA_LIVE:
            total += _dec(r.authorised_cost)
    return total


async def start_replica(db: AsyncSession, service: Service, now: dt.datetime) -> ServiceReplica | None:
    """Create one replica job (same flow as POST /v1/jobs/v2) and place it, preferring a host not already used.

    Returns None when refused (cap, funds, spend limit, validation); ``service.last_error`` says why.
    """
    from app.api.routers.jobs_v2 import build_v2_plan
    from app.services.failover import reschedule_with_failover
    from app.services.kits import SERVICE_MODES, KitError, validate_job_spec_v2
    from app.services.ledger import InsufficientFunds, to_minor
    from app.services.scheduler import JobValidationError, validate_job
    from app.services.spend_limits import SpendLimitExceeded, spend_guard

    try:
        validated = await validate_job_spec_v2(db, service.spec, allowed_kit_modes=SERVICE_MODES)
        spec, kit, eff = validated.spec, validated.kit, validated.effective_resources
        v2_plan = await build_v2_plan(db, service.customer_id, spec, kit)
    except KitError as exc:
        service.last_error = exc.code[:64]
        return None
    per = _dec(settings.SERVICE_REPLICA_MAX_COST)
    if spec.billing.max_cost is not None:
        per = min(per, _dec(spec.billing.max_cost))
    if await committed_cost(db, service) + per > _dec(service.max_total_cost):
        service.capped = True
        service.last_error = "spend_cap_reached"
        return None
    service.capped = False

    replica_id = uuid.uuid4()
    window = max(120, int(settings.SERVICE_REPLICA_WINDOW_SECONDS))
    ends = now + dt.timedelta(seconds=window)
    v2_plan["service"] = {
        "service_id": str(service.id), "replica_id": str(replica_id), "port": service.port,
        "health_path": service.health_path, "window_ends_at": ends.isoformat(),
        "max_request_bytes": int(settings.SERVICE_MAX_REQUEST_BYTES),
        "max_response_bytes": int(settings.SERVICE_MAX_RESPONSE_BYTES),
        "max_concurrency": max(1, min(64, int(service.max_concurrency or 1))),
    }
    gpu_count = int(eff.get("gpu_count") or 1)
    job = Job(
        customer_id=service.customer_id, project_id=service.project_id, workload_type="kit",
        model=f"{kit.slug}@{kit.version}", input={}, status=fsm.CREATED, gpu_count=gpu_count,
        estimated_minutes=max(1, window // 60), max_cost=per, max_runtime_seconds=window + 300,
        retry_policy={"max_retries": 0}, priority=0, execution_mode="hardware", isolation_tier=service.isolation_tier,
        required_gpu_runtime=service.required_gpu_runtime, network_policy=None,
        data_policy={"v2_plan": v2_plan}, idempotency_key=None, machine_id=None,
        spec_v2={"spec": spec.model_dump(mode="json"), "effective_resources": eff,
                 "kit_manifest_sha256": kit.manifest_sha256, "kit_image": kit.image_digest},
        kit_id=kit.slug, kit_version=kit.version,
    )
    db.add(job)
    await db.flush()
    await fsm.transition(db, job, fsm.VALIDATING, reason="service_replica", actor="system")
    try:
        await validate_job(db, job)
    except JobValidationError as exc:
        await fsm.transition(db, job, fsm.FAILED, reason=exc.code, actor="system")
        job.error_code = exc.code
        service.last_error = exc.code[:64]
        return None
    await fsm.transition(db, job, fsm.QUEUED, reason="validated", actor="system")
    avoid = await _machines_in_use(db, service.id)
    try:
        async with spend_guard(db, service.customer_id, service.project_id, to_minor(per)):
            await reschedule_with_failover(db, job, now, exclude=avoid)
    except (SpendLimitExceeded, InsufficientFunds) as exc:
        code = "spend_limit_exceeded" if isinstance(exc, SpendLimitExceeded) else "insufficient_credits"
        await fsm.transition(db, job, fsm.FAILED, reason=code, actor="system")
        job.error_code = code
        service.last_error = code
        return None
    # A job left QUEUED (no host yet) is picked up by the existing recovery sweep; the replica waits as 'starting'.
    replica = ServiceReplica(
        id=replica_id, service_id=service.id, job_id=job.id, machine_id=job.machine_id, status=REPLICA_STARTING,
        window_ends_at=ends, authorised_cost=per, created_at=now,
    )
    db.add(replica)
    await db.flush()
    service.last_error = None
    return replica


# --------------------------------------------------------------------------------------------
# Request queue
# --------------------------------------------------------------------------------------------
async def enqueue_request(
    db: AsyncSession, service: Service, *, method: str, path: str, content_type: str, body: bytes,
    idempotency_key: str | None, no_retry: bool, now: dt.datetime | None = None,
) -> ServiceRequest:
    now = now or utcnow()
    if service.status != SERVICE_ACTIVE:
        raise ServiceError("service_not_active", 409)
    if method not in ("GET", "POST"):
        raise ServiceError("method_not_allowed", 405)
    if not valid_path(path):
        raise ServiceError("invalid_path")
    if not valid_content_type(content_type):
        raise ServiceError("invalid_content_type")
    if len(body) > int(settings.SERVICE_MAX_REQUEST_BYTES):
        raise ServiceError("request_too_large", 413)
    if idempotency_key is not None:
        if len(idempotency_key) > 128:
            raise ServiceError("invalid_idempotency_key")
        prior = (await db.execute(select(ServiceRequest).where(
            ServiceRequest.service_id == service.id, ServiceRequest.idempotency_key == idempotency_key))).scalar_one_or_none()
        if prior is not None:
            return prior
    depth = await db.scalar(select(func.count()).select_from(ServiceRequest).where(
        ServiceRequest.service_id == service.id, ServiceRequest.status == REQ_QUEUED))
    if int(depth or 0) >= MAX_QUEUE_PER_SERVICE:
        raise ServiceError("queue_full", 429)
    req = ServiceRequest(
        service_id=service.id, customer_id=service.customer_id, status=REQ_QUEUED, idempotency_key=idempotency_key,
        method=method, path=path, content_type=content_type, body_b64=base64.b64encode(body).decode("ascii"),
        no_retry=no_retry, queued_at=now,
        deadline_at=now + dt.timedelta(seconds=int(service.request_timeout_seconds)),
    )
    db.add(req)
    service.last_request_at = now
    await db.flush()
    return req


def request_payload(req: ServiceRequest) -> dict:
    return {"request_id": str(req.id), "method": req.method, "path": req.path, "content_type": req.content_type,
            "body_b64": req.body_b64, "attempt": req.attempts, "deadline_at": _aware(req.deadline_at).isoformat()}


def replica_routable(replica: ServiceReplica, now: dt.datetime) -> bool:
    hb = _aware(replica.last_heartbeat_at)
    return (replica.status == REPLICA_HEALTHY and hb is not None
            and (now - hb).total_seconds() <= settings.SERVICE_HEARTBEAT_TIMEOUT_SECONDS)


async def replica_poll(
    db: AsyncSession, replica: ServiceReplica, *, healthy: bool, now: dt.datetime | None = None, want_work: bool = True,
) -> dict:
    """One poll: record the heartbeat and health, then hand out at most one queued request."""
    now = now or utcnow()
    if replica.status == REPLICA_DEAD or replica.status == REPLICA_STOPPED:
        return {"stop": True, "reason": "replica_" + replica.status}
    replica.last_heartbeat_at = now
    if healthy and replica.status == REPLICA_STARTING:
        replica.status = REPLICA_HEALTHY
        replica.healthy_at = now
    elif not healthy and replica.status == REPLICA_HEALTHY:
        replica.status = REPLICA_STARTING  # out of routing until the health check passes again
    if replica.status == REPLICA_DRAINING:
        return {"stop": True, "reason": "draining", "in_flight": replica.in_flight}
    if _aware(replica.window_ends_at) <= now:
        replica.status = REPLICA_DRAINING
        replica.end_reason = replica.end_reason or "window_end"
        return {"stop": True, "reason": "window_end"}
    if not (want_work and replica.status == REPLICA_HEALTHY):
        return {"stop": False, "request": None}
    svc = await db.get(Service, replica.service_id)
    cap = max(1, min(64, int(getattr(svc, "max_concurrency", 1) or 1)))
    if int(replica.in_flight or 0) >= cap:
        return {"stop": False, "request": None}  # at the service's max_concurrency: no more work for this replica
    req = (await db.execute(
        select(ServiceRequest).where(
            ServiceRequest.service_id == replica.service_id, ServiceRequest.status == REQ_QUEUED,
            ServiceRequest.deadline_at > now).order_by(ServiceRequest.queued_at).limit(1).with_for_update(skip_locked=True)
    )).scalar_one_or_none()
    if req is None:
        return {"stop": False, "request": None}
    req.status = REQ_ASSIGNED
    req.replica_id = replica.id
    req.attempts += 1
    req.assigned_at = now
    replica.in_flight = int(replica.in_flight or 0) + 1
    await db.flush()
    return {"stop": False, "request": request_payload(req)}


async def complete_request(
    db: AsyncSession, req: ServiceRequest, replica: ServiceReplica, *, status_code: int, content_type: str | None,
    body: bytes, error_code: str | None = None, now: dt.datetime | None = None,
) -> str:
    """First accepted response wins. Returns 'accepted', 'duplicate' (same replica repeat) or raises 409."""
    now = now or utcnow()
    if req.status in (REQ_DONE, REQ_FAILED) and req.replica_id == replica.id:
        return "duplicate"
    if req.status != REQ_ASSIGNED or req.replica_id != replica.id:
        raise ServiceError("request_not_assigned_to_replica", 409)
    if len(body) > int(settings.SERVICE_MAX_RESPONSE_BYTES):
        body, status_code, error_code = b"", 502, "response_too_large"
    if content_type is not None and not valid_content_type(content_type):
        content_type = "application/octet-stream"
    if not (100 <= status_code <= 599):
        status_code, error_code = 502, "invalid_response_status"
    req.status = REQ_FAILED if error_code else REQ_DONE
    req.response_status = status_code
    req.response_content_type = content_type
    req.response_b64 = base64.b64encode(body).decode("ascii")
    req.response_bytes = len(body)
    req.error_code = error_code
    req.completed_at = now
    replica.in_flight = max(0, int(replica.in_flight or 0) - 1)
    replica.last_heartbeat_at = now
    await db.flush()
    if req.status == REQ_DONE:
        await accrue_request_charge(db, req, replica, now)
    return "accepted"


def decode_response(req: ServiceRequest) -> bytes:
    try:
        return base64.b64decode(req.response_b64 or "", validate=True)
    except (binascii.Error, ValueError):
        return b""


# --------------------------------------------------------------------------------------------
# Per-request billing (SERVICE_BILLING_MODE=per_request). No ledger, pricing or billing code is touched: charges are
# accrued here, and at replica job settlement the existing settle_job meters the job for the request seconds instead
# of uptime (job.metrics.duration_seconds), at the quote's existing per-GPU-second rate. Its hold / capture / release
# stay exactly as they are, so a failed or cancelled replica job still releases its whole hold.
# --------------------------------------------------------------------------------------------
def billing_mode() -> str:
    return "per_request" if str(settings.SERVICE_BILLING_MODE).strip().lower() == "per_request" else "uptime"


def _rate_multiplier() -> decimal.Decimal:
    try:
        m = _dec(settings.SERVICE_PER_REQUEST_RATE_MULTIPLIER)
    except decimal.InvalidOperation:
        return decimal.Decimal(1)
    return m if m >= 0 else decimal.Decimal(1)


TAIL_PENDING = "tail:pending"  # accrued after its replica's batch closed; claimed by the next replica batch of the service
TAIL_UNBILLED = "tail:unbilled"  # no successor and the service is stopped: charged to nobody


def _batch_closed(job: Job | None, replica: ServiceReplica) -> bool:
    if job is None:
        return True
    if (job.metrics or {}).get("billing_batch_key") == f"replica:{replica.id}":
        return True
    return job.status in fsm.TERMINAL_STATES


async def _has_successor(db: AsyncSession, svc: Service | None, replica: ServiceReplica) -> bool:
    """A replica of the same service whose batch is still to close, or a service that can still start one."""
    if svc is not None and svc.status == SERVICE_ACTIVE:
        return True
    live = await db.scalar(select(func.count()).select_from(ServiceReplica).where(
        ServiceReplica.service_id == replica.service_id, ServiceReplica.id != replica.id,
        ServiceReplica.status.in_(REPLICA_LIVE)))
    return int(live or 0) > 0


async def accrue_request_charge(db: AsyncSession, req: ServiceRequest, replica: ServiceReplica, now: dt.datetime) -> ServiceRequestCharge | None:
    """One charge row per completed request: service time (assigned -> completed, capped at the request timeout) times the
    replica's GPU count. Idempotent on request_id. A failed request is never charged."""
    if billing_mode() != "per_request" or req.status != REQ_DONE:
        return None
    prior = (await db.execute(select(ServiceRequestCharge).where(ServiceRequestCharge.request_id == req.id))).scalar_one_or_none()
    if prior is not None:
        return prior
    assigned = _aware(req.assigned_at)
    done = _aware(req.completed_at) or now
    if assigned is None:
        return None
    svc = await db.get(Service, req.service_id)
    cap = decimal.Decimal(int(svc.request_timeout_seconds)) if svc is not None else decimal.Decimal(300)
    secs = min(max(_dec((done - assigned).total_seconds()), decimal.Decimal(0)), cap).quantize(decimal.Decimal("0.001"))
    job = await db.get(Job, replica.job_id)
    gpus = max(1, int(getattr(job, "gpu_count", 1) or 1))
    batch_key: str | None = None
    if _batch_closed(job, replica):
        # Finished after its replica's batch closed: never lost, never double billed. Carried into the NEXT replica's
        # batch of this service (claimed when that batch closes); with no successor and the service stopped it is not
        # billed at all (``tail_unbilled``, shown in reconciliation, never charged to the customer).
        batch_key = TAIL_PENDING if await _has_successor(db, svc, replica) else TAIL_UNBILLED
    row = ServiceRequestCharge(
        service_id=req.service_id, replica_id=replica.id, job_id=replica.job_id, request_id=req.id, service_seconds=secs,
        gpu_count=gpus, gpu_seconds=(secs * gpus).quantize(decimal.Decimal("0.001")), rate_multiplier=_rate_multiplier(),
        batch_key=batch_key, created_at=now)
    db.add(row)
    await db.flush()
    return row


async def apply_per_request_metering(db: AsyncSession, job: Job, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """Called by the agent job-result route BEFORE settle_job for a successful job. For a replica job in per_request mode
    it closes the replica's batch (idempotent: batch_key ``replica:<id>``) and sets job.metrics.duration_seconds to the
    billable request seconds, so the existing settle_job charges request GPU-seconds (it multiplies by gpu_count) rather
    than uptime. Never exceeds the metered uptime (concurrent requests share the GPU). Returns None when not applicable."""
    if billing_mode() != "per_request":
        return None
    replica = (await db.execute(select(ServiceReplica).where(ServiceReplica.job_id == job.id))).scalar_one_or_none()
    if replica is None:
        return None
    now = now or utcnow()
    key = f"replica:{replica.id}"
    # Own accruals, already-claimed rows of this batch (idempotent re-run) and tails carried over from closed replicas.
    rows = (await db.execute(select(ServiceRequestCharge).where(
        ((ServiceRequestCharge.replica_id == replica.id) & ServiceRequestCharge.batch_key.is_(None))
        | (ServiceRequestCharge.batch_key == key)
        | ((ServiceRequestCharge.service_id == replica.service_id) & (ServiceRequestCharge.batch_key == TAIL_PENDING))
    ).with_for_update())).scalars().all()
    for r in rows:
        if r.batch_key != key:
            r.batch_key = key
            r.settled_at = now
    total = sum((_dec(r.service_seconds) * _dec(r.rate_multiplier) for r in rows), decimal.Decimal(0))
    metrics = dict(job.metrics or {})
    uptime = metrics.get("uptime_seconds", metrics.get("duration_seconds"))
    if "uptime_seconds" not in metrics and uptime is not None:
        metrics["uptime_seconds"] = uptime  # keep the true uptime; duration_seconds becomes the billable request seconds
    billable = min(total, _dec(uptime)) if uptime is not None else total
    metrics["duration_seconds"] = float(billable.quantize(decimal.Decimal("0.001")))
    metrics["billing_mode"] = "per_request"
    metrics["billed_request_count"] = len(rows)
    metrics["billing_batch_key"] = key
    job.metrics = metrics
    await db.flush()
    return {"batch_key": key, "requests": len(rows), "billable_seconds": str(billable)}


PENDING_KEY = "settlement_pending"  # job.metrics marker: value "metering" while the replica's metering has not succeeded


def _is_replica_plan(job: Job) -> bool:
    plan = (job.data_policy or {}).get("v2_plan") or {}
    return bool(plan.get("service"))


async def meter_or_defer(db: AsyncSession, job: Job, now: dt.datetime | None = None) -> bool:
    """Result-path wrapper around apply_per_request_metering. Returns True when settlement may proceed (metered, or not a
    per_request replica job) and False when metering FAILED: the job is then marked ``settlement_pending`` = "metering" in
    its metrics and must NOT be settled (no silent fallback to uptime billing); ``sweep_metering`` retries it. Never raises."""
    if billing_mode() != "per_request" or not _is_replica_plan(job):
        return True
    try:
        async with db.begin_nested():
            await apply_per_request_metering(db, job, now)
        return True
    except Exception:
        logger.exception("per-request metering failed for job %s; settlement deferred", job.id)
    try:
        await db.refresh(job)
    except Exception:
        logger.exception("could not refresh job %s after a failed metering", job.id)
    metrics = dict(job.metrics or {})
    metrics[PENDING_KEY] = "metering"
    metrics.setdefault("metering_retries", 0)
    metrics.setdefault("metering_pending_since", (now or utcnow()).isoformat())
    job.metrics = metrics
    return False


def metering_pending(job: Job) -> bool:
    return (job.metrics or {}).get(PENDING_KEY) == "metering"


async def _capped_fallback(db: AsyncSession, job: Job, now: dt.datetime) -> dict[str, Any]:
    """After SERVICE_METERING_MAX_RETRIES: bill the MINIMUM of uptime and the last known request seconds (never more than
    either). Only sets job.metrics (+ batch bookkeeping); the existing settle_job does the money."""
    replica = (await db.execute(select(ServiceReplica).where(ServiceReplica.job_id == job.id))).scalar_one()
    key = f"replica:{replica.id}"
    rows = (await db.execute(select(ServiceRequestCharge).where(
        ServiceRequestCharge.replica_id == replica.id,
        (ServiceRequestCharge.batch_key == key) | ServiceRequestCharge.batch_key.is_(None)).with_for_update())).scalars().all()
    for r in rows:
        if r.batch_key is None:
            r.batch_key = key
            r.settled_at = now
    req_secs = sum((_dec(r.service_seconds) * _dec(r.rate_multiplier) for r in rows), decimal.Decimal(0))
    metrics = dict(job.metrics or {})
    uptime = metrics.get("uptime_seconds", metrics.get("duration_seconds"))
    billable = min(req_secs, _dec(uptime)) if uptime is not None else req_secs
    if uptime is not None:
        metrics["uptime_seconds"] = uptime
    metrics["duration_seconds"] = float(billable.quantize(decimal.Decimal("0.001")))
    metrics["billing_mode"] = "per_request_fallback"
    metrics["metering_fallback"] = "min_uptime_request_seconds"
    metrics["billed_request_count"] = len(rows)
    metrics["billing_batch_key"] = key
    job.metrics = metrics
    await db.flush()
    return {"batch_key": key, "billable_seconds": str(billable)}


async def _finalize_metered_job(db: AsyncSession, job: Job, now: dt.datetime) -> str:
    """Complete a job parked in VERIFYING by a deferred metering: the same non-private tail of the agent result route
    (verify shape, COMPLETED, existing settle_job; or FAILED + release_hold). No ledger logic of its own."""
    from app.api.routers.agents import _post_terminal_hooks
    from app.services import billing
    from app.services import verification as verification_service
    from app.services.audit import record_audit
    from app.services.leases import close_running_attempt, release_job_reservation

    verification = verification_service.verify_output(job.workload_type, job.input, job.output or {})
    job.verification = verification
    if verification["valid"]:
        await fsm.transition(db, job, fsm.COMPLETED, reason="verified", actor="system")
        await close_running_attempt(db, job, now, outcome="completed")
        invoice = None
        if not verification_service.is_unbilled_job(job):
            invoice = await billing.settle_job(db, job)
        await record_audit(db, action="job.complete", outcome="success", resource_type="job", resource_id=str(job.id))
        await _post_terminal_hooks(db, job, invoice=invoice)
    else:
        job.error_code = "output_verification_failed"
        job.error_message = f"verification failed: {verification.get('reason')}"
        await fsm.transition(db, job, fsm.FAILED, reason="verification_failed", actor="system")
        await close_running_attempt(db, job, now, outcome="failed", error_code=job.error_code)
        await billing.release_hold(db, job)
        await record_audit(db, action="job.fail", outcome="failure", resource_type="job", resource_id=str(job.id),
                           reason=job.error_code)
        await _post_terminal_hooks(db, job)
    await release_job_reservation(db, job, now)
    await db.flush()
    return job.status


async def _bump_retries(sm: Any, job_id: uuid.UUID) -> None:
    async with sm() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
        if job is not None and metering_pending(job):
            metrics = dict(job.metrics or {})
            metrics["metering_retries"] = int(metrics.get("metering_retries") or 0) + 1
            job.metrics = metrics
            await db.commit()


async def sweep_metering(sm: Any) -> dict[str, int]:
    """Worker step: retry the metering of replica jobs parked by ``meter_or_defer`` (job in VERIFYING with
    metrics.settlement_pending == "metering"). One transaction and try/except per job, bounded retries. After
    SERVICE_METERING_MAX_RETRIES the capped fallback (min of uptime and request seconds) is applied. Then the job settles
    through the existing billing path."""
    out = {"pending": 0, "metered": 0, "fallback": 0, "retried": 0, "errors": 0}
    if billing_mode() != "per_request":
        return out
    async with sm() as db:
        ids = [j.id for j in (await db.execute(select(Job).where(
            Job.status == fsm.VERIFYING, Job.kit_id.is_not(None)).order_by(Job.created_at).limit(200))).scalars().all()
            if metering_pending(j)]
    max_retries = max(1, int(settings.SERVICE_METERING_MAX_RETRIES))
    for jid in ids:
        out["pending"] += 1
        try:
            async with sm() as db:
                job = (await db.execute(select(Job).where(Job.id == jid).with_for_update(skip_locked=True))).scalar_one_or_none()
                if job is None or job.status != fsm.VERIFYING or not metering_pending(job):
                    continue
                now = utcnow()
                retries = int((job.metrics or {}).get("metering_retries") or 0)
                if retries >= max_retries:
                    await _capped_fallback(db, job, now)
                    kind = "fallback"
                else:
                    async with db.begin_nested():
                        await apply_per_request_metering(db, job, now)
                    kind = "metered"
                metrics = dict(job.metrics or {})
                metrics.pop(PENDING_KEY, None)
                metrics["metering_resolved"] = kind
                job.metrics = metrics
                await _finalize_metered_job(db, job, now)
                await db.commit()
            out[kind] += 1
        except Exception:
            logger.exception("metering retry failed for job %s (counted; retried next sweep)", jid)
            out["errors"] += 1
            try:
                await _bump_retries(sm, jid)
                out["retried"] += 1
            except Exception:
                logger.exception("could not record the metering retry for job %s", jid)
    return out


async def settle_orphan_tails(sm: Any) -> int:
    """Tails carried for a service that is stopped (deleted/paused) with no live replica left can never be claimed:
    mark them ``tail:unbilled`` (not charged; shown in reconciliation)."""
    n = 0
    async with sm() as db:
        sids = list((await db.execute(select(ServiceRequestCharge.service_id).where(
            ServiceRequestCharge.batch_key == TAIL_PENDING).distinct().limit(200))).scalars().all())
        for sid in sids:
            svc = await db.get(Service, sid)
            if svc is not None and svc.status == SERVICE_ACTIVE:
                continue
            live = await db.scalar(select(func.count()).select_from(ServiceReplica).where(
                ServiceReplica.service_id == sid, ServiceReplica.status.in_(REPLICA_LIVE)))
            if int(live or 0) > 0:
                continue
            for c in (await db.execute(select(ServiceRequestCharge).where(
                    ServiceRequestCharge.service_id == sid, ServiceRequestCharge.batch_key == TAIL_PENDING).with_for_update())).scalars().all():
                c.batch_key = TAIL_UNBILLED
                n += 1
        await db.commit()
    return n


# --------------------------------------------------------------------------------------------
# Failover
# --------------------------------------------------------------------------------------------
async def _end_replica(db: AsyncSession, replica: ServiceReplica, status: str, reason: str, now: dt.datetime) -> None:
    replica.status = status
    replica.end_reason = replica.end_reason or reason
    replica.ended_at = replica.ended_at or now
    replica.in_flight = 0


async def _release_requests(db: AsyncSession, replica: ServiceReplica, now: dt.datetime) -> dict[str, int]:
    """Assigned requests of a lost replica: back to the queue ONCE, otherwise failed (never retried twice)."""
    out = {"requeued": 0, "failed": 0}
    rows = (await db.execute(select(ServiceRequest).where(
        ServiceRequest.replica_id == replica.id, ServiceRequest.status == REQ_ASSIGNED).with_for_update())).scalars().all()
    for req in rows:
        if req.no_retry or req.attempts >= MAX_ATTEMPTS or _aware(req.deadline_at) <= now:
            req.status = REQ_EXPIRED if _aware(req.deadline_at) <= now else REQ_FAILED
            req.error_code = "deadline_exceeded" if req.status == REQ_EXPIRED else "replica_lost"
            req.completed_at = now
            out["failed"] += 1
        else:
            req.status = REQ_QUEUED
            req.replica_id = None
            req.assigned_at = None
            out["requeued"] += 1
    return out


async def _cancel_replica_job(db: AsyncSession, job: Job, now: dt.datetime) -> None:
    """Same sequence as the customer cancel (routers/jobs.py::cancel_job): flag, free the reservation, FSM -> CANCELLED,
    release the hold in full. Idempotent: a job already past the cancellable states is left alone."""
    from app.services import billing
    from app.services.leases import release_job_reservation

    locked = (await db.execute(select(Job).where(Job.id == job.id).with_for_update())).scalar_one_or_none() or job
    locked.cancel_requested = True
    if locked.status in fsm.CANCELLABLE_STATES:
        await release_job_reservation(db, locked, now)
        await fsm.transition(db, locked, fsm.CANCELLED, reason="service_replica_start_timeout", actor="system")
        await billing.release_hold(db, locked)  # idempotent per job (release_hold:<job_id>)


async def failover_service(db: AsyncSession, service: Service, now: dt.datetime) -> dict[str, int]:
    """Sync replica state with its job, mark silent replicas dead (out of routing), requeue their requests."""
    from app.services.failover import record_provider_failure

    stats = {"dead": 0, "stopped": 0, "requeued": 0, "failed_requests": 0, "expired": 0}
    timeout = int(settings.SERVICE_HEARTBEAT_TIMEOUT_SECONDS)
    start_grace = timeout * 20
    replicas = (await db.execute(select(ServiceReplica).where(
        ServiceReplica.service_id == service.id, ServiceReplica.status.in_(REPLICA_LIVE)).with_for_update())).scalars().all()
    for r in replicas:
        job = await db.get(Job, r.job_id)
        if job is not None and job.machine_id is not None and r.machine_id != job.machine_id:
            r.machine_id = job.machine_id
        if job is None or job.status in fsm.TERMINAL_STATES or job.status == fsm.VERIFYING:
            lost = job is None or job.status in (fsm.FAILED, fsm.TIMED_OUT, fsm.CANCELLED)
            await _end_replica(db, r, REPLICA_DEAD if lost else REPLICA_STOPPED, f"job_{job.status if job else 'missing'}", now)
            stats["dead" if lost else "stopped"] += 1
            res = await _release_requests(db, r, now)
            stats["requeued"] += res["requeued"]
            stats["failed_requests"] += res["failed"]
            continue
        if r.status == REPLICA_STARTING and r.healthy_at is None and (
                now - _aware(r.created_at)).total_seconds() > int(settings.SERVICE_REPLICA_START_TIMEOUT_SECONDS):
            # Never became healthy in time: fail the replica, cancel its job (hold released in full, never charged); the
            # autoscale step of the same sweep starts a replacement, avoiding this host (see _machines_in_use).
            await _cancel_replica_job(db, job, now)
            await _end_replica(db, r, REPLICA_DEAD, "start_timeout", now)
            stats["dead"] += 1
            stats["start_timeouts"] = stats.get("start_timeouts", 0) + 1
            res = await _release_requests(db, r, now)
            stats["requeued"] += res["requeued"]
            stats["failed_requests"] += res["failed"]
            continue
        hb = _aware(r.last_heartbeat_at)
        if hb is None:
            started = _aware(job.started_at)
            silent = job.status == fsm.RUNNING and started is not None and (now - started).total_seconds() > start_grace
        else:
            silent = (now - hb).total_seconds() > timeout
        if silent:
            await _end_replica(db, r, REPLICA_DEAD, "heartbeat_missed", now)
            stats["dead"] += 1
            res = await _release_requests(db, r, now)
            stats["requeued"] += res["requeued"]
            stats["failed_requests"] += res["failed"]
            try:
                await record_provider_failure(db, job, r.machine_id, reason=f"service replica {r.id} heartbeat missed")
            except Exception:
                logger.exception("service failover: could not record provider failure for replica %s", r.id)
            continue
    # Requests past their deadline never wait forever.
    late = (await db.execute(select(ServiceRequest).where(
        ServiceRequest.service_id == service.id, ServiceRequest.status.in_(REQ_OPEN),
        ServiceRequest.deadline_at <= now).with_for_update(skip_locked=True))).scalars().all()
    for req in late:
        rep = await db.get(ServiceReplica, req.replica_id) if req.replica_id else None
        if rep is not None:
            rep.in_flight = max(0, int(rep.in_flight or 0) - 1)
        req.status = REQ_EXPIRED
        req.error_code = "deadline_exceeded"
        req.completed_at = now
        stats["expired"] += 1
    await db.flush()
    return stats


# --------------------------------------------------------------------------------------------
# Autoscaling controller
# --------------------------------------------------------------------------------------------
def _effective(r: ServiceReplica, now: dt.datetime) -> bool:
    """Counts toward capacity: live, not draining, and not about to reach the end of its window."""
    return (r.status in (REPLICA_STARTING, REPLICA_HEALTHY)
            and (_aware(r.window_ends_at) - now).total_seconds() > ROLLOVER_LEAD_SECONDS)


async def autoscale_service(db: AsyncSession, service: Service, now: dt.datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"started": 0, "drained": 0, "desired": service.desired_replicas}
    replicas = (await db.execute(select(ServiceReplica).where(
        ServiceReplica.service_id == service.id, ServiceReplica.status.in_(REPLICA_LIVE)))).scalars().all()
    # A replica at the end of its window is told to drain (its job result bills the window).
    for r in replicas:
        if r.status != REPLICA_DRAINING and _aware(r.window_ends_at) <= now:
            r.status = REPLICA_DRAINING
            r.end_reason = r.end_reason or "window_end"
    effective = [r for r in replicas if _effective(r, now)]
    healthy = [r for r in effective if replica_routable(r, now)]
    queued_rows = (await db.execute(select(ServiceRequest.queued_at).where(
        ServiceRequest.service_id == service.id, ServiceRequest.status == REQ_QUEUED))).scalars().all()
    queued = len(queued_rows)
    oldest_wait = max(((now - _aware(q)).total_seconds() for q in queued_rows), default=0.0)
    in_flight = sum(int(r.in_flight or 0) for r in replicas)
    floor = service.min_replicas
    ceiling = min(service.max_replicas, int(settings.SERVICE_MAX_REPLICAS_LIMIT))
    last_req = _aware(service.last_request_at) or _aware(service.created_at) or now
    idle_for = (now - last_req).total_seconds()
    last_scaled = _aware(service.last_scaled_at)
    cooled = last_scaled is None or (now - last_scaled).total_seconds() >= settings.SERVICE_SCALE_COOLDOWN_SECONDS

    want = len(effective)
    if service.status != SERVICE_ACTIVE:
        want = 0
    else:
        if queued > 0 and len(effective) == 0:
            want = max(1, floor)  # cold start
        elif queued > 0 and cooled and (
            queued / max(1, len(healthy)) >= settings.SERVICE_SCALE_UP_QUEUE_PER_REPLICA
            or oldest_wait >= settings.SERVICE_SCALE_UP_LATENCY_SECONDS
        ):
            want = len(effective) + 1
            service.last_scaled_at = now
        elif queued == 0 and in_flight == 0 and len(effective) > floor and cooled and idle_for >= settings.SERVICE_SCALE_DOWN_IDLE_SECONDS:
            want = len(effective) - 1
            service.last_scaled_at = now
        if service.scale_to_zero and queued == 0 and in_flight == 0 and idle_for >= settings.SERVICE_IDLE_SCALE_TO_ZERO_SECONDS:
            want = 0
        want = max(floor, min(want, ceiling))
        if want < len(effective) and not (service.scale_to_zero and want == 0) and want < floor:
            want = floor
    service.desired_replicas = want
    out["desired"] = want

    if len(effective) < want:
        for _ in range(want - len(effective)):
            if await start_replica(db, service, now) is None:
                break  # cap, funds, validation: stop trying this sweep
            out["started"] += 1
    elif len(effective) > want:
        # drain the least busy, newest first; never below what is needed
        extras = sorted(effective, key=lambda r: (int(r.in_flight or 0), -(_aware(r.created_at) or now).timestamp()))
        for r in extras[: len(effective) - want]:
            r.status = REPLICA_DRAINING
            r.end_reason = "scale_down"
            out["drained"] += 1
    await db.flush()
    return out


async def scrub_request_payloads(db: AsyncSession, service_id: uuid.UUID, now: dt.datetime) -> int:
    """Blank the stored request body and response of finished requests older than SERVICE_REQUEST_RETENTION_SECONDS.

    The setting used to be declared but never applied, so customer payloads (up to MBs of base64 per request) stayed in
    the database forever. Only the payload columns are cleared: the row, its status and the per-request charge (which
    references the row) stay, so billing and the audit trail are untouched.
    """
    retention = int(settings.SERVICE_REQUEST_RETENTION_SECONDS)
    if retention <= 0:
        return 0
    cutoff = now - dt.timedelta(seconds=retention)
    res = await db.execute(
        update(ServiceRequest)
        .where(
            ServiceRequest.service_id == service_id,
            ServiceRequest.status.notin_(REQ_OPEN),
            ServiceRequest.completed_at.is_not(None),
            ServiceRequest.completed_at < cutoff,
            (ServiceRequest.body_b64 != "") | ServiceRequest.response_b64.is_not(None),
        )
        .values(body_b64="", response_b64=None)
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0)


async def sweep_services(sm: Any) -> dict[str, int]:
    """Worker step: one transaction and one try/except per service so a broken service never blocks the others."""
    totals: dict[str, int] = {"services": 0, "started": 0, "drained": 0, "dead": 0, "requeued": 0, "errors": 0}
    async with sm() as db:
        ids = list((await db.execute(select(Service.id).where(Service.status != SERVICE_DELETED).limit(500))).scalars().all())
    for sid in ids:
        try:
            async with sm() as db:
                svc = (await db.execute(select(Service).where(Service.id == sid).with_for_update(skip_locked=True))).scalar_one_or_none()
                if svc is None:
                    continue
                now = utcnow()
                f = await failover_service(db, svc, now)
                a = await autoscale_service(db, svc, now)
                await scrub_request_payloads(db, svc.id, now)
                await db.commit()
            totals["services"] += 1
            totals["started"] += a["started"]
            totals["drained"] += a["drained"]
            totals["dead"] += f["dead"]
            totals["requeued"] += f["requeued"]
        except Exception:
            logger.exception("service sweep failed for %s (will retry next sweep)", sid)
            totals["errors"] += 1
    try:
        m = await sweep_metering(sm)  # own try/except: a metering problem never blocks autoscaling
        totals["metering_settled"] = m["metered"] + m["fallback"]
        totals["errors"] += m["errors"]
        await settle_orphan_tails(sm)
    except Exception:
        logger.exception("service metering sweep failed (will retry next sweep)")
        totals["errors"] += 1
    return totals


async def retire_service(db: AsyncSession, service: Service, now: dt.datetime | None = None) -> None:
    """Delete: stop routing, drain every replica, fail queued requests. Jobs end through their own results."""
    now = now or utcnow()
    service.status = SERVICE_DELETED
    service.deleted_at = now
    service.desired_replicas = 0
    for r in (await db.execute(select(ServiceReplica).where(
            ServiceReplica.service_id == service.id, ServiceReplica.status.in_(REPLICA_LIVE)))).scalars().all():
        r.status = REPLICA_DRAINING
        r.end_reason = "service_deleted"
    for q in (await db.execute(select(ServiceRequest).where(
            ServiceRequest.service_id == service.id, ServiceRequest.status == REQ_QUEUED))).scalars().all():
        q.status = REQ_FAILED
        q.error_code = "service_deleted"
        q.completed_at = now
    await db.flush()


# --------------------------------------------------------------------------------------------
# Billing reconciliation (read-only)
# --------------------------------------------------------------------------------------------
async def reconcile_service(db: AsyncSession, service_id: uuid.UUID) -> dict[str, Any]:
    """Compare replica metered seconds with what the existing job billing recorded. Changes nothing."""
    replicas = (await db.execute(select(ServiceReplica).where(ServiceReplica.service_id == service_id)
                                 .order_by(ServiceReplica.created_at))).scalars().all()
    min_billable = _dec(settings.BILLING_MIN_BILLABLE_SECONDS)
    tol = decimal.Decimal(str(settings.SERVICE_METER_TOLERANCE))
    slack = decimal.Decimal(int(settings.SERVICE_HEARTBEAT_TIMEOUT_SECONDS))
    rows: list[dict[str, Any]] = []
    totals = {"metered_seconds": 0, "billed_seconds": 0, "billed_minor": 0, "invoiced_minor": 0, "observed_seconds": 0,
              "request_charged_seconds": 0, "request_gpu_seconds": 0, "request_charges": 0}
    per_request = billing_mode() == "per_request"
    unsettled = 0
    mismatches: list[dict[str, str]] = []
    tails = (await db.execute(select(ServiceRequestCharge).where(
        ServiceRequestCharge.service_id == service_id,
        ServiceRequestCharge.batch_key.in_((TAIL_PENDING, TAIL_UNBILLED))))).scalars().all()
    tail_pending = [t for t in tails if t.batch_key == TAIL_PENDING]
    tail_unbilled = [t for t in tails if t.batch_key == TAIL_UNBILLED]
    for r in replicas:
        job = await db.get(Job, r.job_id)
        usage = (await db.execute(select(UsageRecord).where(UsageRecord.job_id == r.job_id))).scalars().first()
        inv = (await db.execute(select(Invoice).where(Invoice.job_id == r.job_id))).scalars().first()
        raw = (job.metrics or {}).get("duration_seconds") if job is not None else None
        metered = _dec(raw) if raw is not None else None
        hb, started = _aware(r.last_heartbeat_at), _aware(job.started_at) if job is not None else None
        observed = _dec(max(0.0, (hb - started).total_seconds())) if hb and started else None
        charges = (await db.execute(select(ServiceRequestCharge).where(
            (ServiceRequestCharge.batch_key == f"replica:{r.id}")  # includes tails carried in from closed replicas
            | ((ServiceRequestCharge.replica_id == r.id) & ServiceRequestCharge.batch_key.is_(None))))).scalars().all()
        if job is not None and (job.metrics or {}).get("settlement_pending") == "metering":
            row_pending = True
        else:
            row_pending = False
        req_secs = sum((_dec(c.service_seconds) * _dec(c.rate_multiplier) for c in charges), decimal.Decimal(0))
        req_gpu = sum((_dec(c.gpu_seconds) for c in charges), decimal.Decimal(0))
        row: dict[str, Any] = {
            "request_charged_seconds": str(req_secs), "request_gpu_seconds": str(req_gpu), "request_charges": len(charges),
            "replica_id": str(r.id), "job_id": str(r.job_id), "status": r.status,
            "metered_seconds": str(metered) if metered is not None else None,
            "observed_seconds": str(observed) if observed is not None else None,
            "billed_seconds": usage.actual_seconds if usage else None,
            "billed_minor": usage.actual_cost_minor if usage else None,
            "invoiced_minor": inv.actual_total_minor if inv else None, "flags": [],
        }
        if row_pending:
            row["flags"].append("metering_pending")
        if usage is None:
            if job is not None and job.status in fsm.TERMINAL_STATES and job.status == fsm.COMPLETED:
                row["flags"].append("completed_without_usage_record")
            else:
                unsettled += 1
        else:
            expected = max(metered if metered is not None else _dec(job.estimated_minutes) * 60, min_billable)
            if per_request and (job.metrics or {}).get("billing_mode") == "per_request_fallback":
                # Metering failed SERVICE_METERING_MAX_RETRIES times: billed min(uptime, request seconds), never more.
                row["flags"].append("metering_fallback_capped")
                expected = max(_dec((job.metrics or {}).get("duration_seconds") or 0), min_billable)
            elif per_request and (job.metrics or {}).get("billing_mode") == "per_request":
                # Billable seconds are the request seconds (capped at uptime), recorded in job.metrics by the result route.
                uptime = (job.metrics or {}).get("uptime_seconds")
                capped = min(req_secs, _dec(uptime)) if uptime is not None else req_secs
                expected = max(capped, min_billable)
                if int(capped) != int(_dec((job.metrics or {}).get("duration_seconds") or 0)):
                    row["flags"].append("request_total_differs_from_metered")
                if any(c.batch_key is None for c in charges):
                    row["flags"].append("request_charges_not_in_batch")
            if int(expected) != int(usage.actual_seconds):
                row["flags"].append("billed_seconds_mismatch")
            if inv is not None and int(inv.actual_total_minor) != int(usage.actual_cost_minor):
                row["flags"].append("invoice_differs_from_usage")
            totals["request_charged_seconds"] += int(req_secs)
            totals["request_gpu_seconds"] += int(req_gpu)
            totals["request_charges"] += len(charges)
            totals["metered_seconds"] += int(metered or 0)
            totals["billed_seconds"] += int(usage.actual_seconds)
            totals["billed_minor"] += int(usage.actual_cost_minor)
            totals["invoiced_minor"] += int(inv.actual_total_minor) if inv else 0
        if metered is not None and observed is not None:
            totals["observed_seconds"] += int(observed)
            if metered > observed * (1 + tol) + slack:
                row["flags"].append("metered_exceeds_observed")
        for f in row["flags"]:
            mismatches.append({"replica_id": str(r.id), "flag": f})
        rows.append(row)
    # Tail requests (finished after their replica's batch closed). pending = waiting for the next replica's batch;
    # unbilled = no successor existed and the service is stopped: charged to nobody, flagged here only.
    if tail_unbilled:
        mismatches.append({"replica_id": "", "flag": "tail_unbilled"})
    totals["tail_pending_requests"] = len(tail_pending)
    totals["tail_pending_seconds"] = str(sum((_dec(t.service_seconds) * _dec(t.rate_multiplier) for t in tail_pending), decimal.Decimal(0)))
    totals["tail_unbilled_requests"] = len(tail_unbilled)
    totals["tail_unbilled_seconds"] = str(sum((_dec(t.service_seconds) * _dec(t.rate_multiplier) for t in tail_unbilled), decimal.Decimal(0)))
    return {"service_id": str(service_id), "billing_mode": billing_mode(), "ok": not mismatches, "unsettled_replicas": unsettled,
            "totals": totals, "mismatches": mismatches, "replicas": rows, "tail_unbilled": len(tail_unbilled)}
