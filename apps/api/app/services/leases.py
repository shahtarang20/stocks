"""Leases, timeouts and recovery (Phase 4).

Acceptance guarantee: a disconnected provider never strands a job. A RUNNING job
carries ``lease_expires_at``; agent heartbeats extend it. When a lease expires
(detected by :func:`recover_expired_leases` — a sweeper, and a lazy check on read):

* the reservation is released (capacity never leaks),
* ``attempts_made`` is incremented,
* if ``attempts_made <= max_retries`` the job goes RUNNING -> RETRY_PENDING ->
  QUEUED and is re-scheduled,
* otherwise it goes RUNNING -> TIMED_OUT.

The reservation is ALWAYS released on any terminal or requeue transition.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_aware, utcnow
from app.models.jobs import Job, JobAssignment, JobAttempt
from app.models.marketplace import ResourceReservation
from app.services import billing
from app.services import job_fsm as fsm
from app.services.scheduler import schedule_job


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
    result = await db.execute(select(Job).where(Job.status == fsm.RUNNING))
    recovered: list[dict] = []
    for job in result.scalars().all():
        exp = ensure_aware(job.lease_expires_at)
        if exp is None or exp > now:
            continue

        await release_job_reservation(db, job, now)
        await close_running_attempt(db, job, now, outcome="lease_expired", error_code="lease_expired")
        job.attempts_made += 1
        max_retries = int((job.retry_policy or {}).get("max_retries", 0) or 0)

        if job.attempts_made <= max_retries:
            await fsm.transition(
                db, job, fsm.RETRY_PENDING, reason="lease_expired", actor="system"
            )
            await fsm.transition(db, job, fsm.QUEUED, reason="retry", actor="system")
            job.machine_id = None
            job.reservation_id = None
            rescheduled = await schedule_job(db, job, now)
            recovered.append(
                {
                    "job_id": str(job.id),
                    "action": "requeued",
                    "attempts_made": job.attempts_made,
                    "rescheduled": rescheduled,
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
