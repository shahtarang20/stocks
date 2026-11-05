"""U16 abuse report and admin review ROUTES through the ASGI client (written, NOT run)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.abuse import AbuseFlag, AbuseHold, AbuseReport
from app.models.enums import Role
from app.models.jobs import Job
from app.services import job_fsm as fsm
from tests.helpers import auth_headers, get_user_id, grant_role, login, register_verify_login

pytestmark = pytest.mark.asyncio

DESC = "This job is mining cryptocurrency on shared hardware."


async def _user(client, email):
    return auth_headers((await register_verify_login(client, email))["access_token"])


async def _admin(client, email="abuse-admin@example.com"):
    await register_verify_login(client, email)
    await grant_role(email, Role.administrator)
    return auth_headers((await login(client, email))["access_token"])


async def _job(uid, status=fsm.RUNNING) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        job = Job(customer_id=uid, workload_type="text-generation", model="m", input={}, status=status, gpu_count=1,
                  estimated_minutes=1, max_runtime_seconds=300, retry_policy={}, execution_mode="hardware")
        db.add(job)
        await db.commit()
        return job.id


async def _flag(uid, job_id=None, status="open") -> uuid.UUID:
    async with get_sessionmaker()() as db:
        f = AbuseFlag(job_id=job_id, user_id=uid, signal="miner_egress", detail={"destinations": ["x:3333"]}, status=status)
        db.add(f)
        await db.commit()
        return f.id


async def test_report_requires_login_and_validates_input(client):
    assert (await client.post("/v1/abuse/report", json={"category": "mining", "description": DESC})).status_code in (401, 403)
    h = await _user(client, "abuse-rep-a@example.com")
    assert (await client.post("/v1/abuse/report", headers=h, json={"category": "mining", "description": "short"})).status_code == 422
    assert (await client.post("/v1/abuse/report", headers=h, json={"category": "spam", "description": DESC})).status_code == 422
    too_long = "x" * (settings.ABUSE_REPORT_MAX_DESCRIPTION + 1)
    assert (await client.post("/v1/abuse/report", headers=h, json={"category": "other", "description": too_long})).status_code == 422


async def test_any_user_can_report_and_it_is_stored_open(client):
    h = await _user(client, "abuse-rep-b@example.com")
    r = await client.post("/v1/abuse/report", headers=h, json={"category": "mining", "description": DESC, "job_id": str(uuid.uuid4())})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "open"
    async with get_sessionmaker()() as db:
        rep = await db.get(AbuseReport, uuid.UUID(r.json()["report_id"]))
        assert rep.category == "mining" and rep.reporter_id == await get_user_id("abuse-rep-b@example.com")


async def test_admin_routes_are_admin_only(client):
    h = await _user(client, "abuse-plain@example.com")
    fid, rid, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    calls = [
        ("get", "/v1/admin/abuse/flags", None), ("get", f"/v1/admin/abuse/flags/{fid}", None),
        ("post", f"/v1/admin/abuse/flags/{fid}/resolve", {"outcome": "dismissed"}),
        ("post", f"/v1/admin/abuse/users/{uid}/release-hold", None), ("get", "/v1/admin/abuse/reports", None),
        ("post", f"/v1/admin/abuse/reports/{rid}/takedown", {"action": "reject"}),
    ]
    for method, url, body in calls:
        r = await getattr(client, method)(url, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (url, r.status_code)


async def test_flags_list_get_and_resolve(client):
    admin = await _admin(client)
    uid = await get_user_id("abuse-admin@example.com")
    jid = await _job(uid)
    fid = await _flag(uid, jid)
    await _flag(uid, status="dismissed")

    listed = await client.get("/v1/admin/abuse/flags", headers=admin)
    assert listed.status_code == 200 and len(listed.json()["flags"]) == 2
    assert listed.json()["enforce_mode"] is settings.ABUSE_ENFORCE
    only_open = await client.get("/v1/admin/abuse/flags", params={"status_filter": "open"}, headers=admin)
    assert [f["id"] for f in only_open.json()["flags"]] == [str(fid)]
    one = await client.get(f"/v1/admin/abuse/flags/{fid}", headers=admin)
    assert one.status_code == 200 and one.json()["signal"] == "miner_egress" and one.json()["job_id"] == str(jid)
    assert (await client.get(f"/v1/admin/abuse/flags/{uuid.uuid4()}", headers=admin)).status_code == 404

    r = await client.post(f"/v1/admin/abuse/flags/{fid}/resolve", headers=admin, json={"outcome": "confirmed", "note": "reviewed"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["review_note"] == "reviewed"
    assert (await client.post(f"/v1/admin/abuse/flags/{fid}/resolve", headers=admin, json={"outcome": "dismissed"})).status_code == 409
    assert (await client.post(f"/v1/admin/abuse/flags/{uuid.uuid4()}/resolve", headers=admin, json={"outcome": "dismissed"})).status_code == 404
    assert (await client.post(f"/v1/admin/abuse/flags/{fid}/resolve", headers=admin, json={"outcome": "maybe"})).status_code == 422


async def test_dismissing_a_flag_releases_its_hold_but_confirming_keeps_it(client):
    admin = await _admin(client)
    uid = await get_user_id("abuse-admin@example.com")
    keep, drop = await _flag(uid), await _flag(uid)
    async with get_sessionmaker()() as db:
        db.add(AbuseHold(user_id=uid, flag_id=keep))
        db.add(AbuseHold(user_id=uid, flag_id=drop))
        await db.commit()
    assert (await client.post(f"/v1/admin/abuse/flags/{keep}/resolve", headers=admin, json={"outcome": "confirmed"})).status_code == 200
    async with get_sessionmaker()() as db:
        holds = {h.flag_id: h for h in (await db.execute(select(AbuseHold))).scalars()}
        assert holds[keep].released_at is None  # a confirmed flag keeps the hold unless the admin asks to release
    assert (await client.post(f"/v1/admin/abuse/flags/{drop}/resolve", headers=admin, json={"outcome": "dismissed"})).status_code == 200
    async with get_sessionmaker()() as db:
        holds = {h.flag_id: h for h in (await db.execute(select(AbuseHold))).scalars()}
        assert holds[drop].released_at is not None and holds[drop].released_by is not None


async def test_release_hold_route(client):
    admin = await _admin(client)
    victim = await get_user_id("abuse-admin@example.com")
    async with get_sessionmaker()() as db:
        db.add(AbuseHold(user_id=victim))
        db.add(AbuseHold(user_id=victim))
        await db.commit()
    r = await client.post(f"/v1/admin/abuse/users/{victim}/release-hold", headers=admin)
    assert r.status_code == 200 and r.json() == {"released": 2}
    assert (await client.post(f"/v1/admin/abuse/users/{victim}/release-hold", headers=admin)).json() == {"released": 0}


async def test_report_takedown_stops_the_job_and_cannot_repeat(client):
    admin = await _admin(client)
    reporter = await _user(client, "abuse-rep-c@example.com")
    owner = await get_user_id("abuse-admin@example.com")
    jid = await _job(owner)
    rid = (await client.post("/v1/abuse/report", headers=reporter, json={"category": "mining", "description": DESC, "job_id": str(jid)})).json()["report_id"]

    listed = await client.get("/v1/admin/abuse/reports", headers=admin)
    assert [r["id"] for r in listed.json()["reports"]] == [rid]
    assert (await client.get("/v1/admin/abuse/reports", params={"status_filter": "rejected"}, headers=admin)).json()["reports"] == []

    r = await client.post(f"/v1/admin/abuse/reports/{rid}/takedown", headers=admin, json={"action": "takedown", "note": "confirmed"})
    assert r.status_code == 200 and r.json()["status"] == "actioned"
    async with get_sessionmaker()() as db:
        job = await db.get(Job, jid)
        assert job.status == fsm.FAILED and job.error_code == "takedown" and job.cancel_requested is True
    assert (await client.post(f"/v1/admin/abuse/reports/{rid}/takedown", headers=admin, json={"action": "reject"})).status_code == 409
    assert (await client.post(f"/v1/admin/abuse/reports/{uuid.uuid4()}/takedown", headers=admin, json={"action": "reject"})).status_code == 404


async def test_reject_report_leaves_the_job_alone(client):
    admin = await _admin(client)
    reporter = await _user(client, "abuse-rep-d@example.com")
    owner = await get_user_id("abuse-admin@example.com")
    jid = await _job(owner)
    rid = (await client.post("/v1/abuse/report", headers=reporter, json={"category": "other", "description": DESC, "job_id": str(jid)})).json()["report_id"]
    r = await client.post(f"/v1/admin/abuse/reports/{rid}/takedown", headers=admin, json={"action": "reject"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    async with get_sessionmaker()() as db:
        assert (await db.get(Job, jid)).status == fsm.RUNNING


async def _hold_for(uid, job_id, minor=250) -> None:
    """The same hold transaction billing.reserve_for_job posts at scheduling."""
    from app.services import ledger

    async with get_sessionmaker()() as db:
        customer = await ledger.get_or_create_customer_account(db, uid)
        holding = await ledger.get_platform_account(db, "holding")
        await ledger.post_transaction(
            db, kind="hold", legs=[(customer, "debit", minor), (holding, "credit", minor)],
            idempotency_key=f"hold:{job_id}", reference_type="job", reference_id=str(job_id), actor_user_id=uid,
        )
        await db.commit()


async def _released(job_id) -> bool:
    from app.services.billing import _txn_by_key

    async with get_sessionmaker()() as db:
        return await _txn_by_key(db, f"release_hold:{job_id}") is not None


@pytest.mark.parametrize("status,expected", [(fsm.RUNNING, fsm.FAILED), (fsm.RESERVED, fsm.CANCELLED)])
async def test_takedown_releases_the_customers_hold_for_any_live_state(client, status, expected):
    """A takedown must not strand the customer's held credits (nothing releases a terminal job's hold later), and a
    reserved job (no legal edge to FAILED) must be stopped too instead of failing the whole takedown with a 409."""
    admin = await _admin(client, f"abuse-admin-{status}@example.com")
    reporter = await _user(client, f"abuse-rep-hold-{status}@example.com")
    owner = await get_user_id(f"abuse-admin-{status}@example.com")
    jid = await _job(owner, status=status)
    await _hold_for(owner, jid)
    rid = (await client.post("/v1/abuse/report", headers=reporter, json={"category": "mining", "description": DESC, "job_id": str(jid)})).json()["report_id"]
    assert not await _released(jid)
    r = await client.post(f"/v1/admin/abuse/reports/{rid}/takedown", headers=admin, json={"action": "takedown"})
    assert r.status_code == 200, r.text
    async with get_sessionmaker()() as db:
        assert (await db.get(Job, jid)).status == expected
    assert await _released(jid)
    # Already terminal now: stopping again is a no-op, not a second release.
    async with get_sessionmaker()() as db:
        from app.services import abuse

        assert await abuse.stop_job(db, await db.get(Job, jid), reason="x", actor="t", error_code="x", error_message="x") is False


async def test_abuse_enforcement_releases_the_hold_too(client, monkeypatch):
    from app.services import abuse

    monkeypatch.setattr(settings, "ABUSE_ENFORCE", True)
    await register_verify_login(client, "abuse-enforce-hold@example.com")
    uid = await get_user_id("abuse-enforce-hold@example.com")
    jid = await _job(uid)
    await _hold_for(uid, jid)
    async with get_sessionmaker()() as db:
        flag = AbuseFlag(job_id=jid, user_id=uid, signal="miner_egress", detail={}, status="open")
        db.add(flag)
        await db.flush()
        await abuse.enforce(db, await db.get(Job, jid), flag)
        await db.commit()
    assert await _released(jid)


async def test_report_endpoint_is_rate_limited_per_user(client):
    h = await _user(client, "abuse-rate@example.com")
    body = {"category": "other", "description": DESC}
    codes = [(await client.post("/v1/abuse/report", headers=h, json=body)).status_code for _ in range(21)]
    assert codes[:20] == [201] * 20 and codes[20] == 429


async def test_abuse_hold_blocks_fanout_sessions_and_services_submit(client, monkeypatch):
    """assert_can_submit was wired into jobs, jobs v2, pipelines and BYOC only: a held user could still start
    fan-outs, sessions and services."""
    h = await _user(client, "held-all@example.com")
    uid = await get_user_id("held-all@example.com")
    async with get_sessionmaker()() as db:
        db.add(AbuseHold(user_id=uid))
        await db.commit()
    monkeypatch.setattr(settings, "ABUSE_ENFORCE", True)
    fan = await client.post("/v1/jobs/v2/fanout", headers=h, json={"spec": {"version": 2}})
    ses = await client.post("/v1/sessions", headers=h, json={
        "image_digest": "x@sha256:" + "0" * 64, "max_price_per_gpu_hour": "1.00", "client_pubkey": "ab" * 32})
    svc = await client.post("/v1/services", headers=h, json={"name": "s", "spec": {}})
    for r in (fan, ses, svc):
        assert r.status_code == 403 and r.json()["detail"] == "submissions_suspended_pending_review", r.text
