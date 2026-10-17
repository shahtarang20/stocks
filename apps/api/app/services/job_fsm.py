"""Job finite-state machine (Phase 4).

ALL job state changes go through :func:`transition`. It validates the edge
against :data:`ALLOWED_TRANSITIONS` (rejecting an illegal edge with HTTP 409 and
leaving state untouched), updates ``job.status``, stamps the relevant timestamps,
and appends an immutable ``job_events`` row with the next per-job sequence number.

State names are stored lowercase (contract response examples use lowercase).
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.jobs import Job, JobEvent

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
CREATED = "created"
VALIDATING = "validating"
QUEUED = "queued"
MATCHED = "matched"
RESERVED = "reserved"
RUNNING = "running"
VERIFYING = "verifying"
COMPLETED = "completed"
FAILED = "failed"
TIMED_OUT = "timed_out"
RETRY_PENDING = "retry_pending"
CANCELLED = "cancelled"
DISPUTED = "disputed"

ALL_STATES = (
    CREATED, VALIDATING, QUEUED, MATCHED, RESERVED, RUNNING, VERIFYING,
    COMPLETED, FAILED, TIMED_OUT, RETRY_PENDING, CANCELLED, DISPUTED,
)

TERMINAL_STATES = frozenset({COMPLETED, FAILED, TIMED_OUT, CANCELLED, DISPUTED})

# Any non-terminal state is cancellable (honored at the next safe point).
CANCELLABLE_STATES = frozenset(
    {CREATED, VALIDATING, QUEUED, MATCHED, RESERVED, RUNNING}
)

# Explicit allowed edges per the Phase 4 contract FSM.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({VALIDATING, CANCELLED}),
    VALIDATING: frozenset({QUEUED, FAILED, CANCELLED}),
    QUEUED: frozenset({MATCHED, FAILED, CANCELLED}),
    MATCHED: frozenset({RESERVED, QUEUED, CANCELLED}),
    RESERVED: frozenset({RUNNING, QUEUED, CANCELLED}),
    RUNNING: frozenset(
        {VERIFYING, FAILED, TIMED_OUT, RETRY_PENDING, CANCELLED}
    ),
    VERIFYING: frozenset({COMPLETED, FAILED, DISPUTED}),
    RETRY_PENDING: frozenset({QUEUED, CANCELLED, TIMED_OUT}),
    COMPLETED: frozenset({DISPUTED}),
    FAILED: frozenset({DISPUTED}),
    TIMED_OUT: frozenset(),
    CANCELLED: frozenset(),
    DISPUTED: frozenset(),
}


def is_allowed(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


async def _next_seq(db: AsyncSession, job_id) -> int:
    result = await db.execute(
        select(func.max(JobEvent.seq)).where(JobEvent.job_id == job_id)
    )
    current = result.scalar_one_or_none()
    return (current or 0) + 1


async def transition(
    db: AsyncSession,
    job: Job,
    to_state: str,
    *,
    reason: str | None = None,
    actor: str = "system",
) -> JobEvent:
    """Transactionally move ``job`` to ``to_state`` and append a job_events row.

    Raises HTTPException(409) for an illegal edge without mutating any state.
    """
    from_state = job.status
    if not is_allowed(from_state, to_state):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"illegal_transition:{from_state}->{to_state}",
        )

    now = utcnow()
    job.status = to_state
    if to_state == RUNNING and job.started_at is None:
        job.started_at = now
    if to_state in TERMINAL_STATES:
        job.finished_at = now
        job.lease_expires_at = None

    seq = await _next_seq(db, job.id)
    event = JobEvent(
        job_id=job.id,
        seq=seq,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        actor=actor,
        created_at=now,
    )
    db.add(event)
    await db.flush()
    return event
