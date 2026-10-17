"""Validation + transparent weighted scheduling (Phase 4).

Validation (while a job is in ``validating``) checks the model, the input schema
for the workload, gpu fit against a targeted machine, and the estimated cost
against the budget cap. Failures raise :class:`JobValidationError`; the caller
transitions the job to ``failed`` and surfaces the HTTP error.

Scheduling (while a job is ``queued``) applies HARD eligibility
(workload+model+gpu+online+available, restricted to a targeted machine if any),
then a transparent WEIGHTED score (price — lower is better, reliability if any,
availability). The best machine is picked and capacity is reserved atomically via
the Phase 3 ``reserve_capacity`` path (no double-book). The choice is recorded in
``job.match_explanation`` so the match is explainable.
"""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import uuid
from dataclasses import dataclass

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.base import utcnow
from app.models.jobs import Job, JobAssignment
from app.models.marketplace import CURRENCY, Quote
from app.models.model_registry import ModelVersion
from app.models.provider import ProviderMachine
from app.services import job_fsm as fsm
from app.services.billing import release_hold, reserve_for_job

logger = logging.getLogger(__name__)
from app.services.ledger import InsufficientFunds
from app.services.marketplace import active_reservation_slots, reserve_capacity
from app.services.pricing import compute_breakdown, get_active_fee_rate
from app.services.provider import derive_status

SUPPORTED_WORKLOADS = ("text-generation", "batch-inference")


class JobValidationError(Exception):
    """Raised on a failed VALIDATING check. Carries the error code + HTTP status."""

    def __init__(self, code: str, http_status: int = status.HTTP_422_UNPROCESSABLE_ENTITY):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Model registry lookup
# ---------------------------------------------------------------------------
async def get_model_version(db: AsyncSession, version_tag: str) -> ModelVersion | None:
    result = await db.execute(
        select(ModelVersion).where(ModelVersion.version_tag == version_tag)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Input schema validation
# ---------------------------------------------------------------------------
def _validate_input(workload_type: str, data: dict) -> None:
    if not isinstance(data, dict):
        raise JobValidationError("invalid_input")
    if workload_type == "text-generation":
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise JobValidationError("invalid_input")
        if "max_tokens" in data and not isinstance(data["max_tokens"], int):
            raise JobValidationError("invalid_input")
    elif workload_type == "batch-inference":
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise JobValidationError("invalid_input")
    else:
        raise JobValidationError("invalid_input")


def _machine_query():
    return select(ProviderMachine).options(
        selectinload(ProviderMachine.hardware),
        selectinload(ProviderMachine.capability),
    )


async def _get_machine(db: AsyncSession, machine_id: uuid.UUID) -> ProviderMachine | None:
    result = await db.execute(_machine_query().where(ProviderMachine.id == machine_id))
    return result.scalar_one_or_none()


async def _reference_price(
    db: AsyncSession, job: Job, machine: ProviderMachine | None
) -> decimal.Decimal | None:
    """Price used to estimate cost for the budget check.

    Targeted machine price when a machine is specified; otherwise the cheapest
    asking price among machines that support the workload. ``None`` if no
    reference exists (budget check is then deferred to scheduling).
    """
    if machine is not None:
        return machine.asking_price_per_hour
    result = await db.execute(_machine_query())
    prices = []
    for m in result.scalars().all():
        if m.status == "deregistered" or m.asking_price_per_hour is None:
            continue
        workloads = m.capability.supported_workloads if m.capability else []
        if job.workload_type in (workloads or []):
            prices.append(decimal.Decimal(m.asking_price_per_hour))
    return min(prices) if prices else None


async def validate_job(db: AsyncSession, job: Job) -> None:
    """Run all VALIDATING checks. Raises JobValidationError on the first failure."""
    # 1) Model approved + supports the workload.
    mv = await get_model_version(db, job.model)
    if (
        mv is None
        or mv.trust_status != "approved"
        or job.workload_type not in (mv.supported_workloads or [])
    ):
        raise JobValidationError("model_not_supported")

    # Record the honest execution mode derived from the model runtime.
    job.execution_mode = "simulated" if mv.runtime == "simulated" else "hardware"

    # 2) Input schema for the workload.
    _validate_input(job.workload_type, job.input)

    # 3) GPU fit against a targeted machine (if specified).
    target: ProviderMachine | None = None
    if job.machine_id is not None:
        target = await _get_machine(db, job.machine_id)
        if target is None:
            raise JobValidationError("machine_not_found", status.HTTP_404_NOT_FOUND)
        machine_gpus = target.hardware.gpu_count if target.hardware else 0
        if job.gpu_count > machine_gpus:
            raise JobValidationError("gpu_count_exceeds_machine")
        target_tiers = (
            target.capability.supported_isolation_tiers
            if target.capability and getattr(target.capability, "supported_isolation_tiers", None)
            else ["container"]
        )
        tier = getattr(job, "isolation_tier", "container") or "container"
        if tier != "container" and tier not in (target_tiers or []):
            raise JobValidationError("isolation_tier_not_supported_by_machine", status.HTTP_409_CONFLICT)

    # 4) Estimated cost <= budget cap.
    if job.max_cost is not None:
        price = await _reference_price(db, job, target)
        if price is not None:
            fee_rate = await get_active_fee_rate(db)
            breakdown = compute_breakdown(
                price_per_hour=price,
                estimated_minutes=job.estimated_minutes,
                gpu_count=job.gpu_count,
                platform_fee_rate=fee_rate,
            )
            if breakdown.total > decimal.Decimal(job.max_cost):
                raise JobValidationError("budget_exceeded", status.HTTP_409_CONFLICT)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
@dataclass
class _Candidate:
    machine: ProviderMachine
    used_slots: int
    free_slots: int
    price: decimal.Decimal


async def _eligible_machines(
    db: AsyncSession, job: Job, now: dt.datetime
) -> list[_Candidate]:
    query = _machine_query()
    if job.machine_id is not None:
        query = query.where(ProviderMachine.id == job.machine_id)
    result = await db.execute(query)
    candidates: list[_Candidate] = []
    for m in result.scalars().all():
        if m.status == "deregistered" or m.asking_price_per_hour is None:
            logger.debug("scheduler skip (deregistered/unpriced): machine=%s", m.id)
            continue
        if m.availability != "online" or derive_status(m, now) != "online":
            logger.debug("scheduler skip (offline): machine=%s avail=%s", m.id, m.availability)
            continue
        workloads = m.capability.supported_workloads if m.capability else []
        if job.workload_type not in (workloads or []):
            logger.debug("scheduler skip (workload): machine=%s", m.id)
            continue
        machine_gpus = m.hardware.gpu_count if m.hardware else 0
        if machine_gpus < job.gpu_count:
            logger.debug("scheduler skip (gpus): machine=%s gpus=%s", m.id, machine_gpus)
            continue
        # Phase 8: Isolation tier matching (ADR-0003: No silent downgrade)
        req_tier = (getattr(job, "isolation_tier", None) or "container").lower()
        machine_tiers = (
            m.capability.supported_isolation_tiers
            if m.capability and getattr(m.capability, "supported_isolation_tiers", None)
            else ["container"]
        )
        if req_tier != "container" and req_tier not in (machine_tiers or []):
            continue
        if req_tier == "confidential":
            if not m.capability or m.capability.verification_status != "VERIFIED":
                continue
        used = await active_reservation_slots(db, m.id, now)
        free = m.max_concurrent_jobs - used
        if free < job.gpu_count:
            continue
        candidates.append(
            _Candidate(
                machine=m,
                used_slots=used,
                free_slots=free,
                price=decimal.Decimal(m.asking_price_per_hour),
            )
        )
    return candidates


def _score_candidates(candidates: list[_Candidate]) -> list[tuple[_Candidate, float, dict]]:
    w_price = settings.SCHEDULER_WEIGHT_PRICE
    w_rel = settings.SCHEDULER_WEIGHT_RELIABILITY
    w_avail = settings.SCHEDULER_WEIGHT_AVAILABILITY

    prices = [float(c.price) for c in candidates]
    min_price, max_price = min(prices), max(prices)
    spread = max_price - min_price

    scored: list[tuple[_Candidate, float, dict]] = []
    for c in candidates:
        price_f = float(c.price)
        # Lower price -> higher score (cheapest == 1.0).
        price_score = 1.0 if spread == 0 else (max_price - price_f) / spread
        # No completed-job history yet -> neutral reliability (honest: 0.5).
        reliability_score = 0.5
        availability_score = (
            c.free_slots / c.machine.max_concurrent_jobs
            if c.machine.max_concurrent_jobs
            else 0.0
        )
        score = (
            w_price * price_score
            + w_rel * reliability_score
            + w_avail * availability_score
        )
        factors = {
            "price_per_hour": f"{c.price:.2f}",
            "price_score": round(price_score, 4),
            "reliability_score": round(reliability_score, 4),
            "availability_score": round(availability_score, 4),
        }
        scored.append((c, score, factors))
    # Highest score wins; tie-break on cheaper price then earliest registration.
    scored.sort(key=lambda t: (-t[1], float(t[0].price), t[0].machine.created_at))
    return scored


async def _create_quote_for_job(
    db: AsyncSession, job: Job, machine: ProviderMachine, now: dt.datetime
) -> Quote:
    fee_rate = await get_active_fee_rate(db)
    price = decimal.Decimal(machine.asking_price_per_hour)
    breakdown = compute_breakdown(
        price_per_hour=price,
        estimated_minutes=job.estimated_minutes,
        gpu_count=job.gpu_count,
        platform_fee_rate=fee_rate,
    )
    quote = Quote(
        customer_id=job.customer_id,
        machine_id=machine.id,
        workload_type=job.workload_type,
        gpu_count=job.gpu_count,
        estimated_minutes=job.estimated_minutes,
        price_per_hour_snapshot=price,
        platform_fee_rate=fee_rate,
        gpu_cost=breakdown.gpu_cost,
        platform_fee=breakdown.platform_fee,
        total=breakdown.total,
        currency=CURRENCY,
        status="active",
        expires_at=now + dt.timedelta(seconds=settings.QUOTE_TTL_SECONDS),
    )
    db.add(quote)
    await db.flush()
    return quote


async def schedule_job(
    db: AsyncSession, job: Job, now: dt.datetime | None = None
) -> bool:
    """Try to match + reserve a machine for a QUEUED job.

    Returns True when the job is RESERVED; False when no machine is eligible (the
    job is left QUEUED). Does NOT count a scheduling failure against retries — see
    :func:`run_scheduler`.
    """
    now = now or utcnow()
    candidates = await _eligible_machines(db, job, now)
    if not candidates:
        return False

    scored = _score_candidates(candidates)
    best, best_score, factors = scored[0]

    # Create the pricing snapshot, then HOLD the authorized amount BEFORE reserving
    # machine capacity (budget protection + no-overspend). The job is still QUEUED
    # here, so an InsufficientFunds refusal can be turned into a clean 402/FAILED by
    # the caller. The hold is idempotent per job (a reschedule never double-holds).
    quote = await _create_quote_for_job(db, job, best.machine, now)
    try:
        await reserve_for_job(db, job, quote)
    except InsufficientFunds:
        quote.status = "cancelled"
        await db.flush()
        raise

    await fsm.transition(db, job, fsm.MATCHED, reason="scheduler_matched", actor="scheduler")

    from fastapi import HTTPException

    try:
        reservation = await reserve_capacity(db, quote=quote, now=now)
    except HTTPException:
        # Lost the slot to a concurrent reserver — fall back to QUEUED.
        await fsm.transition(db, job, fsm.QUEUED, reason="reserve_lost", actor="scheduler")
        return False

    job.machine_id = best.machine.id
    job.reservation_id = reservation.id
    job.match_explanation = {
        "chosen_machine_id": str(best.machine.id),
        "score": round(best_score, 4),
        "weights": {
            "price": settings.SCHEDULER_WEIGHT_PRICE,
            "reliability": settings.SCHEDULER_WEIGHT_RELIABILITY,
            "availability": settings.SCHEDULER_WEIGHT_AVAILABILITY,
        },
        "factors": factors,
        "candidates_considered": len(candidates),
    }
    db.add(
        JobAssignment(
            job_id=job.id,
            machine_id=best.machine.id,
            reservation_id=reservation.id,
            status="active",
        )
    )
    await fsm.transition(db, job, fsm.RESERVED, reason="reserved", actor="scheduler")
    await db.flush()
    return True


async def run_scheduler(
    db: AsyncSession, job: Job, now: dt.datetime | None = None
) -> bool:
    """Scheduling pass that COUNTS a failure against retries (sweeper/retry path).

    On no eligible provider, increments attempts_made and, once retries are
    exhausted, transitions the job QUEUED -> FAILED (no_provider). An
    InsufficientFunds hold refusal fails the job immediately (insufficient_credits).
    """
    try:
        if await schedule_job(db, job, now):
            return True
    except InsufficientFunds:
        await fsm.transition(db, job, fsm.FAILED, reason="insufficient_credits", actor="scheduler")
        job.error_code = "insufficient_credits"
        job.error_message = "insufficient credits to authorize job"
        await db.flush()
        return False
    job.attempts_made += 1
    max_retries = int((job.retry_policy or {}).get("max_retries", 0) or 0)
    if job.attempts_made > max_retries:
        await fsm.transition(db, job, fsm.FAILED, reason="no_provider", actor="scheduler")
        job.error_code = "no_provider"
        job.error_message = "no eligible provider available"
        # Release any outstanding hold so a permanently-unschedulable job never
        # leaks reserved customer credits (concurrency F4).
        await release_hold(db, job)
    await db.flush()
    return False
