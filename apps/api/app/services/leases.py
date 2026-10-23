"""Leases, timeouts and recovery (Phase 4).

Acceptance guarantee: a disconnected provider never strands a job. A RUNNING job
carries ``lease_expires_at``; agent heartbeats extend it. When a lease expires
(detected by :func:`recover_expired_leases` — a sweeper, and a lazy check on read):

* the reservation is released (capacity never leaks),
* ``attempts_made`` is incremented,
* if ``attempts_made <= max_retries`` the job goes RUNNING -> RETRY_PENDING ->
  QUEUED and is re-scheduled onto a DIFFERENT machine when one is eligible
  (see :mod:`app.services.failover`),
* otherwise it goes RUNNING -> TIMED_OUT and the ENTIRE hold is released.

The reservation is ALWAYS released on any terminal or requeue transition, and
the per-job hold is posted exactly once (``hold:<job_id>``) so no number of
retries can charge the customer twice. A job with ``cancel_requested`` is never
retried — it goes straight to CANCELLED with its hold returned.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_aware, utcnow
from app.models.jobs import Job, JobAssignment, JobAttempt
from app.models.marketplace import ResourceReservation
from app.services import billing, failover
from app.services import job_fsm as fsm
from app.services.scheduler import run_scheduler
from app.services.verification import abandon_unbilled_job, is_shadow_job, is_unbilled_job


def _unbilled_placement_is_dead(job: Job, now: dt.datetime) -> bool:
    """True once a directly-placed audit job can no longer plausibly be claimed.

    Unbilled jobs carry no reservation deadline, so the job's own runtime budget
    is used as the grace window (doubled: one budget to be claimed, one to run).
    Without a bound a never-claimed shadow would stay pending forever and its
    RedundancyCheck would never resolve.
    """
    created = ensure_aware(job.created_at)
    if created is None:
        return False
    grace = dt.timedelta(seconds=max(int(job.max_runtime_seconds or 0), 60) * 2)
    return now - created > grace


async def release_job_reservation(
    db: AsyncSession, job: Job, now: dt.datetime | None = None
) -> None:
    """Release the job's active reservation + assignment so capacity is freed."""
    now = now or utcnow()
    if job.reservation_id is not None:
        reservation = await db.get(ResourceReservation, job.reservation_id)
        if reservation is not None and reservation.status == "active":
            reservation.status = "released"
            reservation.released_at = now
    result = await db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == job.id, JobAssignment.status == "active"
        )
    )
    for assignment in result.scalars().all():
        assignment.status = "released"
        assignment.released_at = now
    await db.flush()


async def close_running_attempt(
    db: AsyncSession, job: Job, now: dt.datetime, *, outcome: str, error_code: str | None = None
) -> None:
    result = await db.execute(
        select(JobAttempt)
        .where(JobAttempt.job_id == job.id, JobAttempt.status == "running")
        .order_by(JobAttempt.attempt_number.desc())
    )
    attempt = result.scalars().first()
    if attempt is not None:
        attempt.status = outcome
        attempt.error_code = error_code
        attempt.finished_at = now
    await db.flush()


async def recover_expired_leases(
    db: AsyncSession, now: dt.datetime | None = None
) -> list[dict]:
    """Sweep RUNNING jobs whose lease has expired; requeue or time out.

    Returns one record per recovered job describing the action taken.
    """
    now = now or utcnow()
    # Row-lock the candidates and skip any already locked by a concurrent
    # recovery (sweeper + lazy read), so each job is recovered at most once (H7).
    result = await db.execute(
        select(Job).where(Job.status == fsm.RUNNING).with_for_update(skip_locked=True)
    )
    recovered: list[dict] = []
    for job in result.scalars().all():
        if job.status != fsm.RUNNING:
            continue
        exp = ensure_aware(job.lease_expires_at)
        if exp is None or exp > now:
            continue

        failed_machine_id = job.machine_id
        await release_job_reservation(db, job, now)
        await close_running_attempt(db, job, now, outcome="lease_expired", error_code="lease_expired")
        # Audit fact for the provider that dropped the job (B1). Written before
        # any branch so a cancelled/exhausted job still attributes the failure.
        await failover.record_provider_failure(db, job, failed_machine_id)

        # A customer cancellation beats a retry: a cancelled job is never
        # re-dispatched and is never charged. (cancel_requested can still be set
        # on a RUNNING job if the cancel raced the lease expiry.)
        if job.cancel_requested:
            await fsm.transition(
                db, job, fsm.CANCELLED, reason="cancel_requested", actor="system"
            )
            await billing.release_hold(db, job)
            recovered.append(
                {
                    "job_id": str(job.id),
                    "action": "cancelled",
                    "attempts_made": job.attempts_made,
                }
            )
            await db.flush()
            continue

        job.attempts_made += 1
        max_retries = int((job.retry_policy or {}).get("max_retries", 0) or 0)

        if job.attempts_made <= max_retries:
            await fsm.transition(
                db, job, fsm.RETRY_PENDING, reason="lease_expired", actor="system"
            )
            await fsm.transition(db, job, fsm.QUEUED, reason="retry", actor="system")
            # True failover: prefer a machine that has not already failed this
            # job. The hold is NOT re-posted (idempotent on hold:<job_id>), so
            # a retry can never charge the customer twice.
            exclude = await failover.failed_machine_ids(db, job)
            if failed_machine_id is not None:
                exclude.add(failed_machine_id)
            placement = await failover.reschedule_with_failover(
                db, job, now, exclude=exclude
            )
            recovered.append(
                {
                    "job_id": str(job.id),
                    "action": "requeued",
                    "attempts_made": job.attempts_made,
                    "rescheduled": placement["rescheduled"],
                    "machine_id": placement["machine_id"],
                    "failed_over": placement["failed_over"],
                    "excluded_machines": len(exclude),
                }
            )
        else:
            await fsm.transition(
                db, job, fsm.TIMED_OUT, reason="lease_expired", actor="system"
            )
            job.error_code = "timed_out"
            job.error_message = "lease expired; retries exhausted"
            # A timed-out job is never charged — return the full hold.
            await billing.release_hold(db, job)
            recovered.append(
                {
                    "job_id": str(job.id),
                    "action": "timed_out",
                    "attempts_made": job.attempts_made,
                }
            )
        await db.flush()
    return recovered


async def recover_stuck_reserved(
    db: AsyncSession, now: dt.datetime | None = None
) -> list[dict]:
    """Recover RESERVED jobs that were never claimed and whose reservation has
    expired (H6) — otherwise they sit in RESERVED forever with a permanent hold.

    Releases the stale reservation and requeues the job through ``run_scheduler``
    (which re-reserves, or fails + releases the hold once retries are exhausted).
    """
    now = now or utcnow()
    result = await db.execute(
        select(Job).where(Job.status == fsm.RESERVED).with_for_update(skip_locked=True)
    )
    recovered: list[dict] = []
    for job in result.scalars().all():
        if job.status != fsm.RESERVED:
            continue
        reservation = (
            await db.get(ResourceReservation, job.reservation_id)
            if job.reservation_id is not None
            else None
        )
        res_exp = ensure_aware(reservation.expires_at) if reservation is not None else None
        # Still a live, unexpired reservation — the agent may yet claim it.
        if (
            reservation is not None
            and reservation.status == "active"
            and res_exp is not None
            and res_exp > now
        ):
            continue

        if is_unbilled_job(job):
            # Challenge/shadow jobs are PLACED DIRECTLY onto a machine and hold
            # no ResourceReservation by design, so to this sweeper they look
            # permanently "stuck". They must never be pushed through
            # ``run_scheduler``: for a shadow that path re-places it WITHOUT the
            # different-provider constraint that is the entire point of a
            # redundancy check (a cheating provider would be cleared by comparing
            # its own lie against itself). Leave them for the agent to claim, and
            # only once the placement is unambiguously dead, abandon the audit —
            # a lost audit is inconclusive evidence, never fabricated evidence.
            if _unbilled_placement_is_dead(job, now):
                await abandon_unbilled_job(db, job, now, reason="placement_lost")
                recovered.append({"job_id": str(job.id), "action": "abandoned_unbilled"})
                await db.flush()
            continue

        await release_job_reservation(db, job, now)
        await fsm.transition(
            db, job, fsm.QUEUED, reason="reservation_expired", actor="system"
        )
        job.machine_id = None
        job.reservation_id = None
        rescheduled = await run_scheduler(db, job, now)
        recovered.append(
            {"job_id": str(job.id), "action": "requeued", "rescheduled": rescheduled}
        )
        await db.flush()
    return recovered


async def run_recovery_cycle(db: AsyncSession, now: dt.datetime | None = None) -> dict:
    """One full recovery pass, run by the background sweeper (H5):

    1. recover RUNNING jobs with expired leases,
    2. recover RESERVED jobs whose reservation expired,
    3. re-drive jobs stuck in QUEUED (e.g. requeued when no capacity was free).
    """
    now = now or utcnow()
    leases = await recover_expired_leases(db, now)
    reserved = await recover_stuck_reserved(db, now)
    queued_jobs = (
        await db.execute(
            select(Job).where(Job.status == fsm.QUEUED).with_for_update(skip_locked=True)
        )
    ).scalars().all()
    queued: list[dict] = []
    for job in queued_jobs:
        if is_shadow_job(job):
            # A shadow that lost its placement can only be re-placed by
            # ``_pick_shadow_machine``, which is applied at creation time only.
            # The ordinary scheduler would happily put it back on the primary's
            # own machine, so abandon the check instead of re-placing it blind.
            await abandon_unbilled_job(db, job, now, reason="placement_lost")
            queued.append({"job_id": str(job.id), "rescheduled": False})
            continue
        ok = await run_scheduler(db, job, now)
        queued.append({"job_id": str(job.id), "rescheduled": ok})
    await db.flush()
    return {"leases": leases, "reserved": reserved, "queued": queued}
