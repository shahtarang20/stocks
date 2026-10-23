"""Output verification (Phase 4).

This performs EXECUTION / SHAPE verification of a job's declared output — NOT a
cryptographic proof of useful work. A container exiting 0 is never treated as
proof; we check the output conforms to the workload's expected shape.

* text-generation -> output has ``{"text": <str>}``.
* batch-inference  -> output has a manifest whose item count equals the number of
  input items, and every item carries a per-item ``status``.

Returns a JSON-serializable verification record stored on the job.
"""

from __future__ import annotations

import datetime as dt
import decimal
import difflib
import json
import logging
import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.jobs import Job
from app.models.model_registry import ModelVersion
from app.models.verification import RedundancyCheck, VerificationResult
from app.services import job_fsm as fsm
from app.services.challenge import canonical_hash

logger = logging.getLogger(__name__)

_NOTE = "execution/shape verification only; NOT a cryptographic proof of work"


def verify_output(
    workload_type: str, job_input: dict[str, Any], output: Any
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    valid = True
    reason: str | None = None

    if not isinstance(output, dict):
        return {
            "valid": False,
            "workload_type": workload_type,
            "reason": "output_not_object",
            "checks": {"output_is_object": False},
            "note": _NOTE,
        }

    if workload_type == "text-generation":
        has_text = isinstance(output.get("text"), str)
        checks["output_has_text_str"] = has_text
        valid = has_text
        if not has_text:
            reason = "missing_text_field"

    elif workload_type == "batch-inference":
        items = job_input.get("items")
        expected = len(items) if isinstance(items, list) else None
        # The Phase 4 contract's batch output uses `items`; also accept the
        # synonyms `manifest`/`results` for forward compatibility.
        records = None
        for key in ("items", "manifest", "results"):
            candidate = output.get(key)
            if isinstance(candidate, list):
                records = candidate
                break
        got = len(records) if isinstance(records, list) else None
        count_ok = expected is not None and got == expected
        per_item_ok = isinstance(records, list) and all(
            isinstance(r, dict) and "status" in r for r in records
        )
        checks["expected_items"] = expected
        checks["manifest_items"] = got
        checks["item_count_matches"] = count_ok
        checks["all_items_have_status"] = per_item_ok
        valid = bool(count_ok and per_item_ok)
        if not valid:
            reason = "manifest_mismatch"

    else:
        valid = False
        reason = "unknown_workload"
        checks["known_workload"] = False

    return {
        "valid": valid,
        "workload_type": workload_type,
        "reason": reason,
        "checks": checks,
        "note": _NOTE,
    }


# ===========================================================================
# Phase B2 — anti-cheating verification: redundant execution + model identity
# ===========================================================================
#
# WHY these live beside shape verification: ``verify_output`` above answers
# "does this output look like the right shape?". It cannot answer "did this
# provider actually run the model?". Redundant (shadow) execution answers that
# statistically — the same input is re-run on a DIFFERENT provider's machine and
# the two outputs are compared — and the model-identity check answers it for the
# weights themselves. Both write append-only ``verification_results`` evidence,
# which is what payout holds, slashing and disputes are adjudicated from.

# A non-deterministic workload can never match byte-for-byte, so we fall back to
# a similarity score. At/above this the two runs are treated as the same work.
SIMILARITY_MATCH_THRESHOLD = decimal.Decimal("0.95")


def is_unbilled_job(job: Job) -> bool:
    """True for jobs the CUSTOMER must never be charged for.

    Challenge jobs are platform audits and shadow jobs are our own re-execution
    of work the customer already paid for once. Neither ever takes a ledger
    hold, so settling one would be a charge with no authorization behind it —
    the job result path must skip ``billing.settle_job`` for these.
    """
    return bool(job.is_challenge or job.shadow_of_job_id is not None)


def is_shadow_job(job: Job) -> bool:
    """True for a redundancy re-execution of another customer-paid job."""
    return job.shadow_of_job_id is not None


async def abandon_unbilled_job(
    db: AsyncSession,
    job: Job,
    now: dt.datetime | None = None,
    *,
    reason: str = "placement_lost",
) -> None:
    """Cancel a platform audit job whose placement can no longer be honored.

    WHY abandon rather than reschedule: a shadow's independence constraint
    (:func:`_pick_shadow_machine` — a DIFFERENT provider from the primary) is
    applied only at creation. Re-placing it through the ordinary scheduler could
    put it straight back on the primary's own machine, and a provider that
    returns a canned answer would then be "cleared" by comparing its lie against
    itself. An audit we cannot run independently must resolve ``inconclusive``;
    absence of evidence is never evidence.
    """
    now = now or utcnow()
    if job.status in fsm.TERMINAL_STATES:
        return
    if is_shadow_job(job):
        check = (
            await db.execute(
                select(RedundancyCheck).where(RedundancyCheck.shadow_job_id == job.id)
            )
        ).scalars().first()
        if check is not None and check.status == "pending":
            check.status = "inconclusive"
            detail = dict(check.detail or {})
            detail["abandoned_reason"] = reason
            check.detail = detail
            check.resolved_at = now
    await fsm.transition(db, job, fsm.CANCELLED, reason=reason, actor="system")
    job.machine_id = None
    await db.flush()


def should_shadow(rate: float | None = None) -> bool:
    """Sample decision for redundant re-execution, at REDUNDANCY_SAMPLE_RATE."""
    effective = settings.REDUNDANCY_SAMPLE_RATE if rate is None else rate
    if effective <= 0:
        return False
    if effective >= 1:
        return True
    return secrets.randbelow(2**32) < int(effective * (2**32))


def is_deterministic(job: Job) -> bool:
    """Whether two honest runs of ``job`` must produce identical bytes.

    Simulated execution is a pure function of the input. Hardware inference is
    only deterministic when sampling is switched off (temperature 0 or a pinned
    seed); anything else gets the similarity comparison instead.
    """
    if job.execution_mode == "simulated":
        return True
    data = job.input or {}
    if data.get("seed") is not None:
        return True
    temperature = data.get("temperature")
    try:
        return temperature is not None and decimal.Decimal(str(temperature)) == 0
    except (ArithmeticError, ValueError, TypeError):
        return False


def _comparable_text(output: object) -> str:
    """Flatten an output to text for similarity scoring."""
    if isinstance(output, dict):
        for key in ("text", "output", "content"):
            value = output.get(key)
            if isinstance(value, str):
                return value
    if isinstance(output, str):
        return output
    return json.dumps(output, sort_keys=True, separators=(",", ":"), default=str)


def similarity_score(left: object, right: object) -> decimal.Decimal:
    """Ratio in [0,1] of how alike two outputs are (4dp, never a float column)."""
    ratio = difflib.SequenceMatcher(
        None, _comparable_text(left), _comparable_text(right)
    ).ratio()
    return decimal.Decimal(str(round(ratio, 4)))


async def _record_result(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    kind: str,
    passed: bool,
    expected_hash: str | None = None,
    actual_hash: str | None = None,
    detail: dict | None = None,
) -> VerificationResult:
    result = VerificationResult(
        job_id=job_id,
        kind=kind,
        passed=passed,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        detail=detail,
    )
    db.add(result)
    await db.flush()
    return result


# ---------------------------------------------------------------------------
# 2. Redundant (shadow) execution
# ---------------------------------------------------------------------------
async def _pick_shadow_machine(
    db: AsyncSession, primary: Job, shadow: Job, now: dt.datetime
):
    """Choose a machine for the shadow run on a DIFFERENT provider.

    Different *machine* is not enough: a provider who cheats on one of their
    boxes will cheat identically on another, and two matching lies would clear
    the audit. Requiring a different provider_id makes a match meaningful.
    Reuses the scheduler's eligibility rules so a shadow never lands somewhere
    the primary could not have run.
    """
    from app.services.scheduler import _eligible_machines

    primary_machine_id = primary.machine_id
    primary_provider_id = None
    if primary_machine_id is not None:
        from app.models.provider import ProviderMachine

        machine = await db.get(ProviderMachine, primary_machine_id)
        primary_provider_id = machine.provider_id if machine else None

    candidates = [
        c
        for c in await _eligible_machines(db, shadow, now)
        if c.machine.id != primary_machine_id
        and (primary_provider_id is None or c.machine.provider_id != primary_provider_id)
    ]
    if not candidates:
        return None
    # Cheapest eligible candidate — the platform pays for its own audits.
    return min(candidates, key=lambda c: c.price).machine


async def create_shadow_job(
    db: AsyncSession, primary: Job, now: dt.datetime | None = None
) -> tuple[Job, RedundancyCheck] | None:
    """Re-run a completed paid job on another provider's machine.

    Returns ``None`` when the job is not eligible (already a shadow, a
    challenge, or no independent machine is available). Idempotent per primary
    job: a second call returns the existing (shadow, check) pair.

    The shadow job deliberately takes NO ledger hold and is flagged via
    ``jobs.shadow_of_job_id``, so :func:`is_unbilled_job` keeps it off the
    customer's invoice.
    """
    if primary.is_challenge or primary.shadow_of_job_id is not None:
        return None

    now = now or utcnow()
    existing = (
        await db.execute(
            select(RedundancyCheck).where(RedundancyCheck.primary_job_id == primary.id)
        )
    ).scalars().first()
    if existing is not None:
        shadow_existing = (
            await db.get(Job, existing.shadow_job_id) if existing.shadow_job_id else None
        )
        return (shadow_existing, existing) if shadow_existing else None

    shadow = Job(
        customer_id=primary.customer_id,
        project_id=primary.project_id,
        workload_type=primary.workload_type,
        model=primary.model,
        input=dict(primary.input or {}),
        status=fsm.CREATED,
        gpu_count=primary.gpu_count,
        estimated_minutes=primary.estimated_minutes,
        max_runtime_seconds=primary.max_runtime_seconds,
        execution_mode=primary.execution_mode,
        isolation_tier=primary.isolation_tier,
        shadow_of_job_id=primary.id,
    )
    machine = await _pick_shadow_machine(db, primary, shadow, now)
    if machine is None:
        logger.info("redundancy: no independent machine for job=%s", primary.id)
        return None

    db.add(shadow)
    await db.flush()
    await fsm.transition(db, shadow, fsm.VALIDATING, reason="shadow_created", actor="system")
    await fsm.transition(db, shadow, fsm.QUEUED, reason="shadow_queued", actor="system")
    await fsm.transition(db, shadow, fsm.MATCHED, reason="shadow_matched", actor="scheduler")
    shadow.machine_id = machine.id
    await fsm.transition(db, shadow, fsm.RESERVED, reason="shadow_reserved", actor="scheduler")

    check = RedundancyCheck(
        primary_job_id=primary.id,
        shadow_job_id=shadow.id,
        status="pending",
        detail={
            "primary_machine_id": str(primary.machine_id) if primary.machine_id else None,
            "shadow_machine_id": str(machine.id),
            "deterministic": is_deterministic(primary),
        },
    )
    db.add(check)
    await db.flush()
    return shadow, check


async def maybe_create_shadow_job(
    db: AsyncSession, primary: Job, now: dt.datetime | None = None
) -> tuple[Job, RedundancyCheck] | None:
    """Sampling entry point for redundant execution of a completed paid job."""
    if is_unbilled_job(primary) or not should_shadow():
        return None
    return await create_shadow_job(db, primary, now)


async def resolve_redundancy(
    db: AsyncSession, shadow: Job, output: object
) -> RedundancyCheck | None:
    """Compare a finished shadow run against its primary and record evidence.

    Idempotent on the check's ``status``: once resolved (matched/mismatched),
    a repeated result delivery is a no-op that returns the existing row. A
    primary with no stored output resolves ``inconclusive`` and records NO
    verification result — absence of evidence is not evidence of cheating.
    """
    if shadow.shadow_of_job_id is None:
        return None

    check = (
        await db.execute(
            select(RedundancyCheck).where(RedundancyCheck.shadow_job_id == shadow.id)
        )
    ).scalars().first()
    if check is None or check.status in ("matched", "mismatched", "inconclusive"):
        return check

    primary = await db.get(Job, shadow.shadow_of_job_id)
    if primary is None or primary.output is None:
        check.status = "inconclusive"
        check.resolved_at = utcnow()
        check.detail = {**(check.detail or {}), "reason": "primary_output_missing"}
        await db.flush()
        return check

    primary_hash = canonical_hash(primary.output)
    shadow_hash = canonical_hash(output)
    deterministic = is_deterministic(primary)

    if deterministic:
        matched = secrets.compare_digest(primary_hash, shadow_hash)
        similarity = decimal.Decimal("1.0000") if matched else similarity_score(
            primary.output, output
        )
    else:
        similarity = similarity_score(primary.output, output)
        matched = similarity >= SIMILARITY_MATCH_THRESHOLD

    check.matched = matched
    check.similarity = similarity
    check.status = "matched" if matched else "mismatched"
    check.resolved_at = utcnow()
    check.detail = {
        **(check.detail or {}),
        "deterministic": deterministic,
        "threshold": str(SIMILARITY_MATCH_THRESHOLD),
    }

    # Evidence is attached to the PRIMARY job: that is the paid work whose
    # payout and reputation the outcome affects.
    await _record_result(
        db,
        job_id=primary.id,
        kind="redundant",
        passed=matched,
        expected_hash=primary_hash,
        actual_hash=shadow_hash,
        detail={
            "shadow_job_id": str(shadow.id),
            "similarity": str(similarity),
            "deterministic": deterministic,
        },
    )
    await db.flush()
    return check


# ---------------------------------------------------------------------------
# 3. Model identity
# ---------------------------------------------------------------------------
def normalize_weight_hash(value: str | None) -> str | None:
    """Strip an optional ``sha256:``/``@sha256:`` prefix and lowercase."""
    if not value:
        return None
    text = value.strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    return text or None


async def check_model_identity(
    db: AsyncSession, job: Job, reported_hash: str | None
) -> VerificationResult | None:
    """Validate an agent-reported model weight hash against the registry.

    Returns ``None`` when the agent reported nothing (the field is optional) or
    the registry has no pinned digest to compare against — we never record a
    failure we cannot substantiate. A mismatch is recorded as
    ``kind='model_identity', passed=False``: the provider ran *something else*
    than the model the customer paid for.
    """
    reported = normalize_weight_hash(reported_hash)
    if reported is None:
        return None

    existing = (
        await db.execute(
            select(VerificationResult).where(
                VerificationResult.job_id == job.id,
                VerificationResult.kind == "model_identity",
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing

    version = (
        await db.execute(
            select(ModelVersion).where(ModelVersion.version_tag == job.model)
        )
    ).scalar_one_or_none()
    expected = normalize_weight_hash(version.image_digest if version else None)
    if expected is None:
        logger.info("model identity: no pinned digest for model=%s", job.model)
        return None

    passed = secrets.compare_digest(expected, reported)
    return await _record_result(
        db,
        job_id=job.id,
        kind="model_identity",
        passed=passed,
        expected_hash=expected,
        actual_hash=reported,
        detail={"model": job.model, "machine_id": str(job.machine_id) if job.machine_id else None},
    )
