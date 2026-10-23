"""Provider failover (Phase B1): move a job OFF the machine that just died.

Recovery (``app.services.leases``) already requeues a job whose lease expired.
That alone is not failover: the scheduler would happily hand the job straight
back to the machine that just crashed, so a permanently-dead provider burns
every retry on the same box and the customer sees ``timed_out`` instead of a
result.

This module adds the missing half:

* :func:`failed_machine_ids` — which machines have already failed THIS job,
  read from the immutable ``job_attempts`` log (not from in-memory state, so a
  recovery running in a different process reaches the same conclusion);
* :func:`reschedule_with_failover` — schedule the job preferring any eligible
  machine that is NOT in that set, falling back to the full eligible set when
  no alternative exists (a single-machine marketplace must still retry);
* :func:`record_provider_failure` — append the append-only ``reputation_events``
  row for the provider whose machine dropped the job.

MONEY: nothing here moves money. The per-job hold is posted exactly once by
``billing.reserve_for_job`` under the idempotency key ``hold:<job_id>``, so a
failover reschedule re-quotes and re-reserves CAPACITY without ever posting a
second hold — the customer is authorized once and charged at most once.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.jobs import Job, JobAttempt
from app.models.provider import ProviderMachine
from app.models.reputation import ReputationEvent

# The scheduler's eligibility + scoring are deliberately reused rather than
# re-implemented: failover must pick the SAME way the normal scheduler picks
# (transparent weighted score), only over a reduced candidate set. Duplicating
# the scoring here would let the two paths silently diverge.
from app.services import reputation as reputation_svc
from app.services.scheduler import (
    _eligible_machines,
    _reliability_by_machine,
    _score_candidates,
    schedule_job,
)

logger = logging.getLogger(__name__)

#: ``job_attempts.status`` values that mean "this machine did not deliver".
#: A machine with such an attempt is excluded from the next placement.
FAILED_ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {"lease_expired", "failed", "timed_out", "cancelled"}
)

#: ``reputation_events.kind`` written when a machine lets a lease lapse.
LEASE_EXPIRED_KIND = "lease_expired"


async def failed_machine_ids(db: AsyncSession, job: Job) -> set[uuid.UUID]:
    """Machines that already failed an attempt of ``job``.

    Derived from ``job_attempts`` so the exclusion survives a process restart
    and is identical for the sweeper, the lazy read path and the agent claim
    path (all three can drive recovery).
    """
    rows = (
        await db.execute(
            select(JobAttempt.machine_id).where(
                JobAttempt.job_id == job.id,
                JobAttempt.machine_id.is_not(None),
                JobAttempt.status.in_(FAILED_ATTEMPT_STATUSES),
            )
        )
    ).scalars().all()
    return {mid for mid in rows if mid is not None}


async def record_provider_failure(
    db: AsyncSession,
    job: Job,
    machine_id: uuid.UUID | None,
    *,
    kind: str = LEASE_EXPIRED_KIND,
    reason: str | None = None,
) -> ReputationEvent | None:
    """Append a ``reputation_events`` row for the provider that dropped ``job``.

    Intentionally writes the model row directly instead of calling a reputation
    service: scoring policy (how much a dropped lease costs a provider) lives in
    the reputation service and is owned elsewhere. ``delta`` is left at 0 here —
    this row is the AUDIT fact ("machine X lost job Y's lease at T"); the
    scoring pass is what assigns it a weight. Returns ``None`` when the machine
    (and therefore the provider) can no longer be resolved.
    """
    if machine_id is None:
        return None
    machine = await db.get(ProviderMachine, machine_id)
    if machine is None:
        # Machine was deregistered/deleted mid-flight; nothing to attribute.
        logger.debug("failover: no machine %s to attribute failure to", machine_id)
        return None
    event = ReputationEvent(
        provider_id=machine.provider_id,
        machine_id=machine.id,
        kind=kind,
        delta=0,
        reason=reason or f"lease expired on machine {machine.id} for job {job.id}",
        job_id=job.id,
    )
    db.add(event)
    await db.flush()
    return event


async def reschedule_with_failover(
    db: AsyncSession,
    job: Job,
    now: dt.datetime | None = None,
    *,
    exclude: set[uuid.UUID] | None = None,
) -> dict:
    """Re-place a QUEUED ``job``, preferring a machine outside ``exclude``.

    The job must already be in ``queued`` with its previous reservation
    released. Returns a record describing the placement::

        {"rescheduled": bool, "machine_id": str | None, "failed_over": bool,
         "alternatives": int}

    ``failed_over`` is True only when the job landed on a machine that had NOT
    already failed it.

    Implementation note: the preferred machine is applied by pinning
    ``job.machine_id`` before delegating to :func:`scheduler.schedule_job`,
    which treats a set ``machine_id`` as a hard target. That keeps the hold,
    quote, FSM transitions and atomic capacity reservation on the single
    audited code path instead of forking it.
    """
    now = now or utcnow()
    exclude = exclude or set()

    # Start unpinned: the previous placement must not constrain the next one.
    job.machine_id = None
    job.reservation_id = None

    candidates = await _eligible_machines(db, job, now)
    alternatives = [c for c in candidates if c.machine.id not in exclude]

    if alternatives:
        machine_ids = [c.machine.id for c in alternatives]
        reliability = await _reliability_by_machine(db, machine_ids, now)
        # Same inputs as the normal scheduler (persisted reputation outranks the
        # raw completion rate) so the preferred machine is the one the scheduler
        # would itself have chosen from this reduced set.
        reputation = await reputation_svc.reputation_by_machine(db, machine_ids)
        preferred = _score_candidates(alternatives, reliability, reputation)[0][0].machine.id
        job.machine_id = preferred
        if await schedule_job(db, job, now):
            return {
                "rescheduled": True,
                "machine_id": str(job.machine_id),
                "failed_over": True,
                "alternatives": len(alternatives),
            }
        # Lost the slot to a concurrent reserver between scoring and reserving.
        # Fall through and let the scheduler choose from everything that is
        # still eligible rather than stranding the job.
        logger.info(
            "failover: preferred machine %s unavailable for job %s; retrying unpinned",
            preferred,
            job.id,
        )
        job.machine_id = None
        job.reservation_id = None

    # No alternative (or the alternative evaporated): honest fallback to the
    # normal scheduler over the full eligible set. On a single-machine
    # marketplace this re-selects the machine that failed — which is correct:
    # the alternative is refusing to retry at all.
    ok = await schedule_job(db, job, now)
    return {
        "rescheduled": ok,
        "machine_id": str(job.machine_id) if ok and job.machine_id else None,
        "failed_over": False,
        "alternatives": len(alternatives),
    }
