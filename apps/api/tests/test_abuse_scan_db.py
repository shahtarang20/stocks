"""U16 database-level abuse scan: ``scan_job`` against a real BYOC Job row (written, NOT run).

Flags are created once and de-duplicated; ABUSE_ENFORCE=false (default) is observe-only and never fails the job;
ABUSE_ENFORCE=true fails the job, adds an AbuseHold and ``assert_can_submit`` then blocks the owner.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.abuse import AbuseFlag, AbuseHold
from app.models.jobs import Job
from app.models.telemetry import JobTelemetry
from app.services import abuse
from app.services import job_fsm as fsm
from tests.helpers import get_user_id, register_verify_login

pytestmark = pytest.mark.asyncio

BYOC_BLOCK = {"image": "ghcr.io/acme/tool@sha256:" + "a" * 64, "isolation": "gvisor"}


async def _job(db, uid, *, byoc=True, status=fsm.RUNNING) -> Job:
    job = Job(
        customer_id=uid, workload_type="byoc" if byoc else "text-generation", model="byoc", input={}, status=status,
        gpu_count=1, estimated_minutes=1, max_runtime_seconds=300, retry_policy={}, execution_mode="hardware",
        spec_v2={"byoc": dict(BYOC_BLOCK)} if byoc else None,
    )
    db.add(job)
    await db.flush()
    return job


def _stratum_sample(job_id) -> JobTelemetry:
    return JobTelemetry(
        job_id=job_id, timestamp=utcnow() - dt.timedelta(seconds=20), gpu_util_pct=40.0, mem_used_mb=1000,
        step_count=1, extra_metrics={"blocked_egress": ["stratum+tcp://pool.example.org:3333"]},
    )


async def _setup(client, email):
    await register_verify_login(client, email)
    return await get_user_id(email)


async def test_byoc_job_gets_flags_and_they_are_deduplicated(client, monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", False)
    uid = await _setup(client, "abuse-scan-a@example.com")
    async with get_sessionmaker()() as db:
        job = await _job(db, uid)
        db.add(_stratum_sample(job.id))
        await db.commit()
        jid = job.id
    async with get_sessionmaker()() as db:
        job = await db.get(Job, jid)
        first = await abuse.scan_job(db, job)
        await db.commit()
        signals = {f.signal for f in first}
        assert abuse.SIG_BYOC_STRATUM in signals
        assert abuse.SIG_MINER_EGRESS in signals  # the generic signal fires on the same destination
        assert all(f.status == "open" and f.user_id == uid and f.job_id == jid for f in first)
    async with get_sessionmaker()() as db:
        job = await db.get(Job, jid)
        again = await abuse.scan_job(db, job)  # same evidence: nothing new
        await db.commit()
        assert again == []
        rows = (await db.execute(select(AbuseFlag).where(AbuseFlag.job_id == jid))).scalars().all()
        assert len(rows) == len(signals)


async def test_non_byoc_job_never_gets_byoc_signals(client, monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", False)
    uid = await _setup(client, "abuse-scan-b@example.com")
    async with get_sessionmaker()() as db:
        job = await _job(db, uid, byoc=False)
        db.add(_stratum_sample(job.id))
        await db.commit()
        jid = job.id
    async with get_sessionmaker()() as db:
        flags = await abuse.scan_job(db, await db.get(Job, jid))
        await db.commit()
        assert abuse.SIG_BYOC_STRATUM not in {f.signal for f in flags}
        assert abuse.SIG_MINER_EGRESS in {f.signal for f in flags}


async def test_observe_only_does_not_fail_the_job_or_hold_the_user(client, monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", False)
    uid = await _setup(client, "abuse-scan-c@example.com")
    async with get_sessionmaker()() as db:
        job = await _job(db, uid)
        db.add(_stratum_sample(job.id))
        await db.commit()
        jid = job.id
    async with get_sessionmaker()() as db:
        flags = await abuse.scan_job(db, await db.get(Job, jid))
        await db.commit()
        assert flags and not any(f.enforced for f in flags)
    async with get_sessionmaker()() as db:
        job = await db.get(Job, jid)
        assert job.status == fsm.RUNNING and not job.cancel_requested and job.error_code is None
        assert (await db.execute(select(AbuseHold).where(AbuseHold.user_id == uid))).first() is None
        await abuse.assert_can_submit(db, uid)  # no hold, and enforcement is off: no exception


async def test_enforce_fails_the_job_adds_a_hold_and_blocks_submission(client, monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", True)
    uid = await _setup(client, "abuse-scan-d@example.com")
    async with get_sessionmaker()() as db:
        job = await _job(db, uid)
        db.add(_stratum_sample(job.id))
        await db.commit()
        jid = job.id
    async with get_sessionmaker()() as db:
        flags = await abuse.scan_job(db, await db.get(Job, jid))
        await db.commit()
        assert flags and flags[0].enforced is True
    async with get_sessionmaker()() as db:
        job = await db.get(Job, jid)
        assert job.status == fsm.FAILED and job.cancel_requested is True and job.error_code == "abuse_suspected"
        holds = (await db.execute(select(AbuseHold).where(AbuseHold.user_id == uid))).scalars().all()
        assert len(holds) == 1 and holds[0].released_at is None
        assert await abuse.has_submission_hold(db, uid) is True
        with pytest.raises(HTTPException) as e:
            await abuse.assert_can_submit(db, uid)
        assert e.value.status_code == 403 and e.value.detail == "submissions_suspended_pending_review"


async def test_hold_does_not_block_when_enforcement_is_switched_off(client, monkeypatch):
    uid = await _setup(client, "abuse-scan-e@example.com")
    async with get_sessionmaker()() as db:
        db.add(AbuseHold(user_id=uid))
        await db.commit()
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", False)
    async with get_sessionmaker()() as db:
        await abuse.assert_can_submit(db, uid)
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", True)
    async with get_sessionmaker()() as db:
        with pytest.raises(HTTPException):
            await abuse.assert_can_submit(db, uid)


async def test_sweep_scans_only_running_jobs(client, monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", False)
    uid = await _setup(client, "abuse-scan-f@example.com")
    async with get_sessionmaker()() as db:
        running = await _job(db, uid)
        queued = await _job(db, uid, status=fsm.QUEUED)
        db.add(_stratum_sample(running.id))
        db.add(_stratum_sample(queued.id))
        await db.commit()
        running_id, queued_id = running.id, queued.id
    async with get_sessionmaker()() as db:
        n = await abuse.sweep(db)
        await db.commit()
        assert n >= 1
        ids = {f.job_id for f in (await db.execute(select(AbuseFlag))).scalars().all()}
        assert running_id in ids and queued_id not in ids
