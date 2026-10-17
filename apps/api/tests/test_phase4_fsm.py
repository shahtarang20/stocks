"""Phase 4 — FSM engine unit tests (legal + illegal transitions, event log)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.jobs import Job, JobEvent
from app.services import job_fsm as fsm

pytestmark = pytest.mark.asyncio


async def _make_job(s, **kw) -> Job:
    job = Job(
        customer_id=uuid.uuid4(),
        workload_type="text-generation",
        model="echo-sim-v1",
        input={"prompt": "hi"},
        status=fsm.CREATED,
        gpu_count=1,
        estimated_minutes=1,
        max_runtime_seconds=300,
        retry_policy={"max_retries": 0},
        **kw,
    )
    s.add(job)
    await s.flush()
    return job


async def test_legal_transition_chain_appends_sequential_events(db_engine):
    sm = get_sessionmaker()
    async with sm() as s:
        job = await _make_job(s)
        for to in (fsm.VALIDATING, fsm.QUEUED, fsm.MATCHED, fsm.RESERVED, fsm.RUNNING):
            await fsm.transition(s, job, to, reason="step", actor="system")
        await s.commit()
        assert job.status == fsm.RUNNING
        assert job.started_at is not None

        events = (
            await s.execute(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.seq))
        ).scalars().all()
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]
        assert events[0].from_state == fsm.CREATED
        assert events[-1].to_state == fsm.RUNNING


async def test_illegal_transition_rejected_409_and_no_mutation(db_engine):
    sm = get_sessionmaker()
    async with sm() as s:
        job = await _make_job(s)
        await s.commit()
        with pytest.raises(HTTPException) as exc:
            await fsm.transition(s, job, fsm.COMPLETED, actor="system")
        assert exc.value.status_code == 409
        # State unchanged; no event appended.
        assert job.status == fsm.CREATED
        events = (
            await s.execute(select(JobEvent).where(JobEvent.job_id == job.id))
        ).scalars().all()
        assert events == []


async def test_terminal_state_stamps_finished_and_clears_lease(db_engine):
    sm = get_sessionmaker()
    async with sm() as s:
        job = await _make_job(s)
        for to in (fsm.VALIDATING, fsm.QUEUED, fsm.MATCHED, fsm.RESERVED, fsm.RUNNING):
            await fsm.transition(s, job, to, actor="system")
        await fsm.transition(s, job, fsm.VERIFYING, actor="agent")
        await fsm.transition(s, job, fsm.COMPLETED, actor="system")
        await s.commit()
        assert job.status == fsm.COMPLETED
        assert job.finished_at is not None
        assert job.lease_expires_at is None


async def test_allowed_transitions_cover_contract_edges():
    # Spot-check the contract's explicit edges.
    assert fsm.is_allowed(fsm.RUNNING, fsm.RETRY_PENDING)
    assert fsm.is_allowed(fsm.RETRY_PENDING, fsm.QUEUED)
    assert fsm.is_allowed(fsm.RUNNING, fsm.TIMED_OUT)
    assert fsm.is_allowed(fsm.VERIFYING, fsm.COMPLETED)
    assert fsm.is_allowed(fsm.VERIFYING, fsm.FAILED)
    for s in (fsm.CREATED, fsm.VALIDATING, fsm.QUEUED, fsm.MATCHED, fsm.RESERVED, fsm.RUNNING):
        assert fsm.is_allowed(s, fsm.CANCELLED)
    # Illegal examples.
    assert not fsm.is_allowed(fsm.COMPLETED, fsm.RUNNING)
    assert not fsm.is_allowed(fsm.QUEUED, fsm.RUNNING)
