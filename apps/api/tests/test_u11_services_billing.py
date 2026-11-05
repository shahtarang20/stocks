"""U11 services: per-request billing accrual, tail carry-forward, metering failure retry, replica start timeout and
reconciliation flags (docs/architecture/U11-services-design.md). Written, NOT run.

Pure DB tests on the services models (no HTTP); the ledger, pricing and settle_job are never exercised or changed here.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.models.services import (
    REPLICA_DEAD, REPLICA_HEALTHY, REPLICA_STARTING, REQ_DONE, REQ_FAILED, SERVICE_ACTIVE, SERVICE_DELETED, Service,
    ServiceReplica, ServiceRequest, ServiceRequestCharge,
)
from app.models.user import User
from app.services import job_fsm as fsm
from app.services import service_runtime as rt

pytestmark = pytest.mark.asyncio

D = decimal.Decimal


@pytest.fixture(autouse=True)
def _per_request(monkeypatch):
    monkeypatch.setattr(settings, "SERVICE_BILLING_MODE", "per_request")
    monkeypatch.setattr(settings, "SERVICE_PER_REQUEST_RATE_MULTIPLIER", "1")


async def _user(db) -> uuid.UUID:
    u = User(email=f"u11-{uuid.uuid4().hex[:8]}@example.com", password_hash="pw", email_verified=True)
    db.add(u)
    await db.flush()
    return u.id


async def _service(db, uid, *, status=SERVICE_ACTIVE) -> Service:
    svc = Service(customer_id=uid, name="svc", status=status, spec={}, kit_ref="k@1", min_replicas=1, max_replicas=2,
                  max_total_cost=D("50.00"), request_timeout_seconds=60, max_concurrency=2)
    db.add(svc)
    await db.flush()
    return svc


async def _replica(db, svc, *, job_status=fsm.RUNNING, metrics=None, rstatus=REPLICA_HEALTHY, created=None,
                   machine=None) -> tuple[ServiceReplica, Job]:
    now = utcnow()
    job = Job(customer_id=svc.customer_id, workload_type="kit", model="k@1", input={}, status=job_status, gpu_count=1,
              estimated_minutes=1, max_runtime_seconds=300, retry_policy={}, execution_mode="hardware", kit_id="k",
              metrics=metrics, data_policy={"v2_plan": {"service": {"service_id": str(svc.id)}}}, machine_id=machine)
    db.add(job)
    await db.flush()
    rep = ServiceReplica(service_id=svc.id, job_id=job.id, machine_id=machine, status=rstatus,
                         window_ends_at=now + dt.timedelta(hours=1), authorised_cost=D("10.00"),
                         created_at=created or now, healthy_at=now if rstatus == REPLICA_HEALTHY else None)
    db.add(rep)
    await db.flush()
    return rep, job


async def _done_request(db, svc, rep, secs=10, status=REQ_DONE) -> ServiceRequest:
    now = utcnow()
    req = ServiceRequest(service_id=svc.id, customer_id=svc.customer_id, replica_id=rep.id, status=status,
                         queued_at=now - dt.timedelta(seconds=secs + 1), assigned_at=now - dt.timedelta(seconds=secs),
                         completed_at=now, deadline_at=now + dt.timedelta(seconds=60), attempts=1)
    db.add(req)
    await db.flush()
    return req


async def _charges(db, svc_id):
    return (await db.execute(select(ServiceRequestCharge).where(ServiceRequestCharge.service_id == svc_id))).scalars().all()


# ----------------------------------------------------------------------------------------------------------------------
# Per-request accrual idempotency
# ----------------------------------------------------------------------------------------------------------------------
async def test_accrual_is_idempotent_and_failed_requests_are_free(db_engine):
    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db))
        rep, _job = await _replica(db, svc)
        ok = await _done_request(db, svc, rep, secs=10)
        bad = await _done_request(db, svc, rep, secs=5, status=REQ_FAILED)
        first = await rt.accrue_request_charge(db, ok, rep, utcnow())
        again = await rt.accrue_request_charge(db, ok, rep, utcnow())
        assert first is not None and again is not None and first.id == again.id
        assert await rt.accrue_request_charge(db, bad, rep, utcnow()) is None
        rows = await _charges(db, svc.id)
        assert len(rows) == 1 and rows[0].request_id == ok.id and rows[0].batch_key is None
        assert D(rows[0].service_seconds) == D("10.000")


async def test_uptime_mode_accrues_nothing(db_engine, monkeypatch):
    monkeypatch.setattr(settings, "SERVICE_BILLING_MODE", "uptime")
    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db))
        rep, _job = await _replica(db, svc)
        req = await _done_request(db, svc, rep)
        assert await rt.accrue_request_charge(db, req, rep, utcnow()) is None
        assert await _charges(db, svc.id) == []


async def test_close_batch_is_idempotent_and_capped_at_uptime(db_engine):
    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db))
        rep, job = await _replica(db, svc, metrics={"duration_seconds": 12})
        for secs in (10, 10):
            await rt.accrue_request_charge(db, await _done_request(db, svc, rep, secs=secs), rep, utcnow())
        first = await rt.apply_per_request_metering(db, job)
        second = await rt.apply_per_request_metering(db, job)
        assert first["requests"] == second["requests"] == 2
        assert job.metrics["billing_mode"] == "per_request" and job.metrics["uptime_seconds"] == 12
        assert job.metrics["duration_seconds"] == 12.0  # 20 request seconds never exceed the 12 s of uptime
        assert all(c.batch_key == f"replica:{rep.id}" for c in await _charges(db, svc.id))


# ----------------------------------------------------------------------------------------------------------------------
# Tail carry-forward
# ----------------------------------------------------------------------------------------------------------------------
async def test_request_finishing_after_batch_closed_is_carried_into_next_replica_batch(db_engine):
    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db))
        a, job_a = await _replica(db, svc, metrics={"duration_seconds": 100})
        await rt.apply_per_request_metering(db, job_a)  # batch of A closed (no charges yet)
        assert job_a.metrics["billing_batch_key"] == f"replica:{a.id}"
        late = await _done_request(db, svc, a, secs=7)
        row = await rt.accrue_request_charge(db, late, a, utcnow())
        assert row.batch_key == rt.TAIL_PENDING  # not lost, not attached to A's closed batch
        assert (await rt.accrue_request_charge(db, late, a, utcnow())).id == row.id  # idempotent on request_id
        b, job_b = await _replica(db, svc, metrics={"duration_seconds": 100})
        out = await rt.apply_per_request_metering(db, job_b)
        assert out["requests"] == 1 and job_b.metrics["duration_seconds"] == 7.0
        await db.refresh(row)
        assert row.batch_key == f"replica:{b.id}" and row.settled_at is not None
        again = await rt.apply_per_request_metering(db, job_b)  # re-run: same batch, same total, no double count
        assert again["requests"] == 1 and job_b.metrics["duration_seconds"] == 7.0
        assert len(await _charges(db, svc.id)) == 1


async def test_tail_without_successor_on_stopped_service_is_unbilled_and_flagged(db_engine):
    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db), status=SERVICE_DELETED)
        a, job_a = await _replica(db, svc, metrics={"duration_seconds": 50}, rstatus=REPLICA_DEAD)
        await rt.apply_per_request_metering(db, job_a)
        late = await _done_request(db, svc, a, secs=9)
        row = await rt.accrue_request_charge(db, late, a, utcnow())
        assert row.batch_key == rt.TAIL_UNBILLED
        # A later replica of the same service never claims it: the customer is charged nothing for it.
        b, job_b = await _replica(db, svc, metrics={"duration_seconds": 50})
        out = await rt.apply_per_request_metering(db, job_b)
        assert out["requests"] == 0 and job_b.metrics["duration_seconds"] == 0.0
        report = await rt.reconcile_service(db, svc.id)
        assert report["tail_unbilled"] == 1 and report["totals"]["tail_unbilled_requests"] == 1
        assert {"replica_id": "", "flag": "tail_unbilled"} in report["mismatches"] and report["ok"] is False


async def test_orphan_tails_of_a_stopped_service_become_unbilled(db_engine):
    sm = get_sessionmaker()
    async with sm() as db:
        svc = await _service(db, await _user(db))
        a, job_a = await _replica(db, svc, metrics={"duration_seconds": 50}, rstatus=REPLICA_DEAD)
        await rt.apply_per_request_metering(db, job_a)
        row = await rt.accrue_request_charge(db, await _done_request(db, svc, a), a, utcnow())
        assert row.batch_key == rt.TAIL_PENDING  # service still active: a successor can still start
        svc.status = SERVICE_DELETED
        await db.commit()
        sid, rid = svc.id, row.id
    assert await rt.settle_orphan_tails(sm) == 1
    async with sm() as db:
        assert (await db.get(ServiceRequestCharge, rid)).batch_key == rt.TAIL_UNBILLED
        assert sid is not None


# ----------------------------------------------------------------------------------------------------------------------
# Metering failure: deferred, retried, capped fallback (no silent uptime billing)
# ----------------------------------------------------------------------------------------------------------------------
async def test_failed_metering_marks_job_settlement_pending(db_engine, monkeypatch):
    async def boom(db, job, now=None):
        raise RuntimeError("metering down")

    monkeypatch.setattr(rt, "apply_per_request_metering", boom)
    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db))
        _rep, job = await _replica(db, svc, metrics={"duration_seconds": 300})
        assert await rt.meter_or_defer(db, job, utcnow()) is False
        assert rt.metering_pending(job) and job.metrics["metering_retries"] == 0
        assert job.metrics["duration_seconds"] == 300  # untouched: nothing was billed from the uptime figure


async def test_non_replica_or_uptime_mode_proceeds_to_settlement(db_engine, monkeypatch):
    async with get_sessionmaker()() as db:
        uid = await _user(db)
        plain = Job(customer_id=uid, workload_type="kit", model="k@1", input={}, status=fsm.RUNNING, gpu_count=1,
                    estimated_minutes=1, max_runtime_seconds=300, retry_policy={}, execution_mode="hardware", kit_id="k")
        db.add(plain)
        await db.flush()
        assert await rt.meter_or_defer(db, plain, utcnow()) is True
        monkeypatch.setattr(settings, "SERVICE_BILLING_MODE", "uptime")
        svc = await _service(db, uid)
        _rep, job = await _replica(db, svc, metrics={"duration_seconds": 5})
        assert await rt.meter_or_defer(db, job, utcnow()) is True and not rt.metering_pending(job)


async def _parked(db, retries, uptime=100, req_secs=(8, 4)):
    svc = await _service(db, await _user(db))
    rep, job = await _replica(db, svc, job_status=fsm.VERIFYING, metrics={
        "duration_seconds": uptime, "settlement_pending": "metering", "metering_retries": retries})
    for s in req_secs:
        await rt.accrue_request_charge(db, await _done_request(db, svc, rep, secs=s), rep, utcnow())
    await db.commit()
    return svc, rep, job.id


async def test_sweep_retries_then_settles_when_metering_recovers(db_engine, monkeypatch):
    sm = get_sessionmaker()
    finalized: list[uuid.UUID] = []

    async def fake_finalize(db, job, now):
        finalized.append(job.id)
        return job.status

    monkeypatch.setattr(rt, "_finalize_metered_job", fake_finalize)
    async with sm() as db:
        _svc, _rep, jid = await _parked(db, retries=0)
    real = rt.apply_per_request_metering
    state = {"fail": True}

    async def flaky(db, job, now=None):
        if state["fail"]:
            raise RuntimeError("still down")
        return await real(db, job, now)

    monkeypatch.setattr(rt, "apply_per_request_metering", flaky)
    out = await rt.sweep_metering(sm)
    assert out["errors"] == 1 and finalized == []
    async with sm() as db:
        job = await db.get(Job, jid)
        assert job.metrics["metering_retries"] == 1 and rt.metering_pending(job)  # counted, still not settled
    state["fail"] = False
    out = await rt.sweep_metering(sm)
    assert out["metered"] == 1 and finalized == [jid]
    async with sm() as db:
        job = await db.get(Job, jid)
        assert not rt.metering_pending(job) and job.metrics["billing_mode"] == "per_request"
        assert job.metrics["duration_seconds"] == 12.0  # request seconds, not the 100 s of uptime


async def test_capped_fallback_bills_minimum_of_uptime_and_request_seconds(db_engine, monkeypatch):
    sm = get_sessionmaker()
    monkeypatch.setattr(settings, "SERVICE_METERING_MAX_RETRIES", 3)
    finalized: list[uuid.UUID] = []

    async def fake_finalize(db, job, now):
        finalized.append(job.id)
        return job.status

    monkeypatch.setattr(rt, "_finalize_metered_job", fake_finalize)

    async def boom(db, job, now=None):
        raise RuntimeError("down")

    monkeypatch.setattr(rt, "apply_per_request_metering", boom)
    async with sm() as db:
        _s, _r, low = await _parked(db, retries=3, uptime=100, req_secs=(8, 4))  # requests (12) < uptime (100)
        _s, _r, high = await _parked(db, retries=3, uptime=5, req_secs=(8, 4))  # uptime (5) < requests (12)
        _s, _r, early = await _parked(db, retries=1)  # below the cap: stays pending
    out = await rt.sweep_metering(sm)
    assert out["fallback"] == 2 and set(finalized) == {low, high}
    async with sm() as db:
        j_low, j_high, j_early = await db.get(Job, low), await db.get(Job, high), await db.get(Job, early)
        assert j_low.metrics["duration_seconds"] == 12.0 and j_high.metrics["duration_seconds"] == 5.0  # never overcharge
        assert j_low.metrics["billing_mode"] == "per_request_fallback" and not rt.metering_pending(j_low)
        assert rt.metering_pending(j_early) and j_early.metrics["metering_retries"] == 2


async def test_reconcile_flags_pending_and_fallback(db_engine):
    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db))
        _rep, job = await _replica(db, svc, job_status=fsm.VERIFYING, metrics={"settlement_pending": "metering"})
        report = await rt.reconcile_service(db, svc.id)
        assert report["replicas"][0]["flags"] == ["metering_pending"]
        assert {"replica_id": str(_rep.id), "flag": "metering_pending"} in report["mismatches"]


# ----------------------------------------------------------------------------------------------------------------------
# Replica start timeout: dead, job cancelled, hold released, replacement on another host
# ----------------------------------------------------------------------------------------------------------------------
async def test_start_timeout_kills_replica_cancels_job_releases_hold_and_replaces_elsewhere(db_engine, monkeypatch):
    from app.services import billing

    released: list[uuid.UUID] = []

    async def fake_release_hold(db, job):
        released.append(job.id)

    async def fake_release_reservation(db, job, now=None):
        return None

    monkeypatch.setattr(billing, "release_hold", fake_release_hold)
    import app.services.leases as leases

    monkeypatch.setattr(leases, "release_job_reservation", fake_release_reservation)
    monkeypatch.setattr(settings, "SERVICE_REPLICA_START_TIMEOUT_SECONDS", 600)
    bad_machine = uuid.uuid4()
    started: list[set] = []

    async def fake_start(db, service, now):
        started.append(await rt._machines_in_use(db, service.id))
        return None

    async with get_sessionmaker()() as db:
        svc = await _service(db, await _user(db))
        now = utcnow()
        rep, job = await _replica(db, svc, job_status=fsm.RUNNING, rstatus=REPLICA_STARTING, machine=bad_machine,
                                  created=now - dt.timedelta(seconds=700))
        fresh, fresh_job = await _replica(db, svc, job_status=fsm.RUNNING, rstatus=REPLICA_STARTING,
                                          created=now - dt.timedelta(seconds=30))
        stats = await rt.failover_service(db, svc, now)
        assert stats["start_timeouts"] == 1 and stats["dead"] == 1
        assert rep.status == REPLICA_DEAD and rep.end_reason == "start_timeout" and rep.in_flight == 0
        assert job.status == fsm.CANCELLED and job.cancel_requested is True and released == [job.id]
        assert fresh.status == REPLICA_STARTING and fresh_job.status == fsm.RUNNING  # young replica untouched
        monkeypatch.setattr(rt, "start_replica", fake_start)
        svc.min_replicas = 2
        await rt.autoscale_service(db, svc, now)
        assert started and bad_machine in started[0]  # the replacement is placed with the failed host excluded


async def test_request_payloads_scrubbed_after_retention(db_engine, monkeypatch):
    """SERVICE_REQUEST_RETENTION_SECONDS was declared but never applied: bodies stayed in the DB forever."""
    monkeypatch.setattr(settings, "SERVICE_REQUEST_RETENTION_SECONDS", 3600)
    sm = get_sessionmaker()
    async with sm() as db:
        uid = await _user(db)
        svc = await _service(db, uid)
        rep, _job = await _replica(db, svc)
        now = utcnow()

        def mk(status, done_ago):
            return ServiceRequest(
                service_id=svc.id, customer_id=uid, replica_id=rep.id, status=status, body_b64="aGk=", response_b64="b2s=",
                queued_at=now - dt.timedelta(hours=3), completed_at=(now - dt.timedelta(seconds=done_ago)) if done_ago is not None else None,
                deadline_at=now + dt.timedelta(minutes=5), attempts=1)

        old, fresh, open_req = mk(REQ_DONE, 7200), mk(REQ_DONE, 60), mk("assigned", None)
        db.add_all([old, fresh, open_req])
        await db.flush()
        assert await rt.scrub_request_payloads(db, svc.id, now) == 1
        await db.commit()
    async with sm() as db:
        rows = {r.id: r for r in (await db.execute(select(ServiceRequest))).scalars().all()}
        assert rows[old.id].body_b64 == "" and rows[old.id].response_b64 is None and rows[old.id].status == REQ_DONE
        assert rows[fresh.id].body_b64 == "aGk=" and rows[fresh.id].response_b64 == "b2s="
        assert rows[open_req.id].body_b64 == "aGk="


def test_service_path_and_content_type_refuse_newlines():
    """``$`` accepted a trailing newline and ``\\s`` a newline inside the content type: request-line / header injection
    into the customer's service container (review-all)."""
    from app.services import service_runtime as rt

    assert rt.valid_path("/v1/ok") and rt.valid_content_type("application/json") and rt.valid_content_type("text/plain; charset=utf-8")
    for bad in ("/x\n", "/x\r\n", "/x\nHost: evil", "/a//b", "/../x", "x"):
        assert not rt.valid_path(bad), bad
    for bad in ("application/json\n", "application/json;\nX-Evil: 1", "application/json\r\nX: y", "a/b;\tc=d"):
        assert not rt.valid_content_type(bad), bad
