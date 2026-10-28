"""Known-answer challenges (Phase B2) — the anti-cheating audit layer.

WHY this exists: a provider is paid for *claiming* to have run a model. Shape
verification (:mod:`app.services.verification`) only proves the output *looks*
right, so a provider that returns a canned ``{"text": "..."}`` without running
anything still gets paid. A known-answer challenge is a job whose correct output
we already know: it is injected into the normal queue at
``settings.CHALLENGE_SAMPLE_RATE`` and is **indistinguishable** from paid work
(the agent claim payload carries no ``is_challenge`` flag), so the only way to
pass one reliably is to actually execute the model.

Two invariants this module must never break:

* ``jobs.is_challenge`` NEVER reaches the agent API. It is a server-side column
  only; the claim response schema (:class:`app.schemas.jobs.ClaimResponse`) has
  no field for it.
* A challenge job is platform-funded, never billed: no hold is ever taken for
  it, so ``billing.settle_job`` must be skipped for it (see
  :func:`app.services.verification.is_unbilled_job`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.jobs import Job
from app.models.verification import VerificationChallenge, VerificationResult
from app.services import job_fsm as fsm

logger = logging.getLogger(__name__)

# Where the challenge linkage is stashed on the job row. ``jobs.verification``
# is a server-only JSON column (never echoed to the agent in the claim payload),
# which makes it the right place to remember which challenge produced this job.
_LINK_KEY = "challenge_id"


def canonical_hash(value: Any) -> str:
    """sha256 of a canonical JSON encoding of ``value``.

    Canonical = sorted keys + tight separators, so ``{"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` hash identically. Without this, key ordering noise from a
    provider's JSON encoder would look like cheating.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def hash_for_policy(output: Any, config: dict[str, Any] | None) -> str:
    """Hash ``output`` under a challenge's comparison policy.

    * ``{"mode": "exact"}`` (default) — hash the whole output object.
    * ``{"mode": "field", "field": "text"}`` — hash only that top-level field,
      so incidental metadata (timings, request ids) does not fail the check.
    """
    cfg = config or {}
    mode = str(cfg.get("mode") or "exact").lower()
    if mode == "field":
        field = str(cfg.get("field") or "text")
        subject = output.get(field) if isinstance(output, dict) else None
        return canonical_hash(subject)
    return canonical_hash(output)


def should_inject_challenge(rate: float | None = None) -> bool:
    """Sample decision for injecting a challenge, at CHALLENGE_SAMPLE_RATE.

    ``secrets``-backed randomness (not :mod:`random`'s Mersenne Twister) so a
    provider observing past injections cannot predict the next one.
    """
    effective = settings.CHALLENGE_SAMPLE_RATE if rate is None else rate
    if effective <= 0:
        return False
    if effective >= 1:
        return True
    # 2**-32 resolution is far finer than any realistic sample rate.
    return secrets.randbelow(2**32) < int(effective * (2**32))


async def create_challenge(
    db: AsyncSession,
    *,
    workload_type: str,
    model: str,
    input: dict[str, Any],
    expected_output: Any | None = None,
    expected_output_hash: str | None = None,
    config: dict[str, Any] | None = None,
    label: str | None = None,
    active: bool = True,
) -> VerificationChallenge:
    """Persist a challenge. Either the expected output or its hash is required.

    Storing only the hash is supported so an operator can seed challenges whose
    plaintext answer never touches our database (less to leak).
    """
    cfg = dict(config or {})
    if expected_output_hash is None:
        if expected_output is None:
            raise ValueError("expected_output or expected_output_hash is required")
        expected_output_hash = hash_for_policy(expected_output, cfg)
    challenge = VerificationChallenge(
        workload_type=workload_type,
        model=model,
        input=input,
        expected_output_hash=expected_output_hash.lower(),
        config=cfg,
        label=label,
        active=active,
    )
    db.add(challenge)
    await db.flush()
    return challenge


async def select_challenge(
    db: AsyncSession, *, workload_type: str, model: str
) -> VerificationChallenge | None:
    """Pick a random ACTIVE challenge matching the workload + model, or None.

    Random rather than round-robin: a predictable rotation lets a provider
    fingerprint the canned inputs and memorize the answers.
    """
    rows = (
        await db.execute(
            select(VerificationChallenge).where(
                VerificationChallenge.active.is_(True),
                VerificationChallenge.workload_type == workload_type,
                VerificationChallenge.model == model,
            )
        )
    ).scalars().all()
    if not rows:
        return None
    return rows[random.randrange(len(rows))]


async def inject_challenge_job(
    db: AsyncSession,
    *,
    challenge: VerificationChallenge,
    customer_id: uuid.UUID,
    machine_id: uuid.UUID | None = None,
    execution_mode: str = "simulated",
    max_runtime_seconds: int = 300,
) -> Job:
    """Create a challenge job that looks exactly like ordinary queued work.

    ``customer_id`` is the platform's own audit account — a challenge is never
    attributed to, or paid for by, a real customer. No ledger hold is taken, so
    the job is unbillable by construction (there is nothing to capture).

    When ``machine_id`` is given the job is placed straight on that machine
    (RESERVED) so a specific provider can be audited; otherwise it is left
    QUEUED for the ordinary scheduler to match.
    """
    job = Job(
        customer_id=customer_id,
        workload_type=challenge.workload_type,
        model=challenge.model,
        input=dict(challenge.input or {}),
        status=fsm.CREATED,
        execution_mode=execution_mode,
        max_runtime_seconds=max_runtime_seconds,
        is_challenge=True,
        # Server-side linkage only; never serialized into the claim payload.
        verification={_LINK_KEY: str(challenge.id)},
    )
    db.add(job)
    await db.flush()

    await fsm.transition(db, job, fsm.VALIDATING, reason="challenge_injected", actor="system")
    await fsm.transition(db, job, fsm.QUEUED, reason="challenge_queued", actor="system")
    if machine_id is not None:
        await fsm.transition(db, job, fsm.MATCHED, reason="challenge_targeted", actor="scheduler")
        job.machine_id = machine_id
        await fsm.transition(db, job, fsm.RESERVED, reason="challenge_reserved", actor="scheduler")
    await db.flush()
    return job


async def maybe_inject_challenge(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    workload_type: str,
    model: str,
    machine_id: uuid.UUID | None = None,
    execution_mode: str = "simulated",
) -> Job | None:
    """Sampling entry point: roll the dice, then inject if a challenge exists."""
    if not should_inject_challenge():
        return None
    challenge = await select_challenge(db, workload_type=workload_type, model=model)
    if challenge is None:
        return None
    return await inject_challenge_job(
        db,
        challenge=challenge,
        customer_id=customer_id,
        machine_id=machine_id,
        execution_mode=execution_mode,
    )


async def _linked_challenge(db: AsyncSession, job: Job) -> VerificationChallenge | None:
    """Resolve the challenge behind a challenge job.

    Primary path: the id stashed in ``job.verification``. Fallback: match an
    active challenge on (model, workload_type, input) — needed because the job
    result path overwrites ``job.verification`` with the shape-verification
    record, and the canned inputs are unique per challenge in practice.
    """
    link = (job.verification or {}).get(_LINK_KEY)
    if link:
        try:
            challenge = await db.get(VerificationChallenge, uuid.UUID(str(link)))
        except (ValueError, TypeError):
            challenge = None
        if challenge is not None:
            return challenge

    rows = (
        await db.execute(
            select(VerificationChallenge).where(
                VerificationChallenge.workload_type == job.workload_type,
                VerificationChallenge.model == job.model,
            )
        )
    ).scalars().all()
    for candidate in rows:
        if (candidate.input or {}) == (job.input or {}):
            return candidate
    return None


async def evaluate_challenge_job(
    db: AsyncSession, job: Job, output: Any
) -> VerificationResult | None:
    """Score a finished challenge job's output against the known answer.

    Returns None when ``job`` is not a challenge (the ordinary paid path) or the
    originating challenge can no longer be resolved. Idempotent: a repeated
    result delivery for the same job returns the already-recorded evidence
    instead of writing a second row.
    """
    if not job.is_challenge:
        return None

    existing = (
        await db.execute(
            select(VerificationResult).where(
                VerificationResult.job_id == job.id,
                VerificationResult.kind == "challenge",
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing

    challenge = await _linked_challenge(db, job)
    if challenge is None:
        return None

    # The known answer for the runtime this machine used: CUDA and ROCm builds may legitimately
    # differ. No answer recorded for that runtime = inconclusive (no result, reputation unchanged).
    runtime = "cuda"
    if job.machine_id is not None and job.gpu_count:
        from app.services.gpu_runtime import gpu_runtime_of_machine

        runtime = await gpu_runtime_of_machine(db, job.machine_id) or "cuda"
    by_runtime = challenge.expected_hashes_by_runtime or {}
    expected = (by_runtime.get(runtime) or (challenge.expected_output_hash if runtime == "cuda" else "") or "").lower()
    if not expected:
        logger.info("challenge %s: no expected hash for runtime %s; inconclusive", challenge.id, runtime)
        return None
    actual = hash_for_policy(output, challenge.config)
    passed = secrets.compare_digest(actual, expected)

    result = VerificationResult(
        job_id=job.id,
        challenge_id=challenge.id,
        kind="challenge",
        passed=passed,
        expected_hash=expected,
        actual_hash=actual,
        detail={
            "mode": (challenge.config or {}).get("mode", "exact"),
            "challenge_label": challenge.label,
            "machine_id": str(job.machine_id) if job.machine_id else None,
        },
    )
    db.add(result)
    await db.flush()
    return result
