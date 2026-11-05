"""U15 unit-quote ROUTES and quote learning against the database (written, NOT run).

Money-adjacent but ledger-free: quotes are estimates only. Kits are inserted directly (no signing).
"""

from __future__ import annotations

import decimal
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.models.kits import Kit
from app.models.unit_quotes import UnitQuoteSample
from app.services import job_fsm as fsm
from app.services import unit_quotes as uq
from tests.helpers import auth_headers, get_user_id, register_verify_login

pytestmark = pytest.mark.asyncio
D = decimal.Decimal
HINTS = {"billing_hints": {"unit": "frame", "est_gpu_seconds_per_unit": 30}}


@pytest.fixture
def units_on(monkeypatch):
    monkeypatch.setattr(settings, "PRICING_UNITS_ENABLED", True)


async def _kit(slug="uq-kit", version="1.0.0", manifest=None, status="published") -> str:
    async with get_sessionmaker()() as db:
        if (await db.execute(select(Kit).where(Kit.slug == slug, Kit.version == version))).scalar_one_or_none() is None:
            db.add(Kit(slug=slug, version=version, name=slug, category="render", image_digest="x@sha256:" + "0" * 64,
                       manifest=manifest if manifest is not None else HINTS, manifest_sha256="0" * 64,
                       signature="0" * 128, signing_key_id="test", status=status))
            await db.commit()
    return f"{slug}@{version}"


async def _login(client, email):
    return auth_headers((await register_verify_login(client, email))["access_token"])


async def _job(uid, *, max_cost="5.00", kit=None, units=None, seconds=None, gpus=1) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        job = Job(customer_id=uid, workload_type="kit", model="k@1", input={}, status=fsm.COMPLETED, gpu_count=gpus,
                  estimated_minutes=1, max_runtime_seconds=300, retry_policy={}, execution_mode="hardware",
                  max_cost=D(max_cost))
        if kit:
            job.kit_id, job.kit_version = kit
            job.spec_v2 = {"spec": {"billing": {"expected_units": units}}}
            job.metrics = {"duration_seconds": seconds}
        db.add(job)
        await db.commit()
        return job.id


def _body(ref, **over):
    b = {"kit": ref, "expected_units": 100, "max_cost": "50.00", "price_per_hour": "3.6"}
    b.update(over)
    return b


async def test_routes_are_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "PRICING_UNITS_ENABLED", False)
    h = await _login(client, "uq-off@example.com")
    qid = uuid.uuid4()
    for r in (
        await client.post("/v1/unit-quotes", headers=h, json=_body("uq-kit@1.0.0")),
        await client.get(f"/v1/unit-quotes/{qid}", headers=h),
        await client.post(f"/v1/unit-quotes/{qid}/link", headers=h, json={"job_id": str(uuid.uuid4())}),
    ):
        assert r.status_code == 404 and r.json()["detail"] == "not_found"


async def test_create_and_get(client, units_on):
    h = await _login(client, "uq-a@example.com")
    ref = await _kit()
    r = await client.post("/v1/unit-quotes", headers=h, json=_body(ref))
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["kit"] == ref and q["unit"] == "frame" and q["expected_units"] == 100 and q["confidence"] == "low"
    assert q["currency"] == "CREDITS" and q["capped"] is False and q["job_id"] is None
    assert D(q["quote"]["gpu"]["low"]) < D(q["quote"]["gpu"]["expected"]) < D(q["quote"]["gpu"]["high"])
    assert q["quote"]["estimated_gpu_seconds"] == "3000.00"
    got = await client.get(f"/v1/unit-quotes/{q['quote_id']}", headers=h)
    assert got.status_code == 200 and got.json()["quote_id"] == q["quote_id"]
    other = await _login(client, "uq-a-other@example.com")
    assert (await client.get(f"/v1/unit-quotes/{q['quote_id']}", headers=other)).status_code == 404
    assert (await client.get(f"/v1/unit-quotes/{uuid.uuid4()}", headers=h)).status_code == 404


async def test_request_validation_and_kit_errors(client, units_on):
    h = await _login(client, "uq-val@example.com")
    ref = await _kit()
    for bad in (_body("no-at-sign"), _body(ref, expected_units=0), _body(ref, max_cost="0"), _body(ref, gpu_count=0),
                _body(ref, price_per_hour="-1"), {**_body(ref), "surprise": 1}):
        assert (await client.post("/v1/unit-quotes", headers=h, json=bad)).status_code == 422, bad
    r = await client.post("/v1/unit-quotes", headers=h, json=_body("nope@1.0.0"))
    assert r.status_code == 404 and r.json()["detail"] == "kit_not_found"
    nohint = await _kit("uq-nohint", manifest={"billing_hints": {}})
    r = await client.post("/v1/unit-quotes", headers=h, json=_body(nohint))
    assert r.status_code == 422 and r.json()["detail"] == "kit_has_no_billing_hint"
    yanked = await _kit("uq-yanked", status="yanked")
    r = await client.post("/v1/unit-quotes", headers=h, json=_body(yanked))
    assert r.status_code in (404, 409)


async def test_quote_never_shows_more_than_the_cap(client, units_on):
    h = await _login(client, "uq-cap@example.com")
    ref = await _kit()
    r = await client.post("/v1/unit-quotes", headers=h, json=_body(ref, expected_units=100000, max_cost="2.00"))
    assert r.status_code == 201
    q = r.json()
    assert q["capped"] is True and q["max_cost"] == "2.00"
    assert all(D(v) <= D("2.00") for v in q["quote"]["gpu"].values())


async def test_link_to_job_rules(client, units_on):
    h = await _login(client, "uq-link@example.com")
    uid = await get_user_id("uq-link@example.com")
    ref = await _kit()
    qid = (await client.post("/v1/unit-quotes", headers=h, json=_body(ref, max_cost="5.00"))).json()["quote_id"]
    ok_job = await _job(uid, max_cost="5.00")
    over_job = await _job(uid, max_cost="5.01")
    second_job = await _job(uid, max_cost="1.00")

    r = await client.post(f"/v1/unit-quotes/{qid}/link", headers=h, json={"job_id": str(over_job)})
    assert r.status_code == 409 and r.json()["detail"] == "job_cap_exceeds_quote_cap"
    r = await client.post(f"/v1/unit-quotes/{qid}/link", headers=h, json={"job_id": str(ok_job)})
    assert r.status_code == 200 and r.json()["job_id"] == str(ok_job)
    assert (await client.post(f"/v1/unit-quotes/{qid}/link", headers=h, json={"job_id": str(ok_job)})).status_code == 200  # same job: idempotent
    r = await client.post(f"/v1/unit-quotes/{qid}/link", headers=h, json={"job_id": str(second_job)})
    assert r.status_code == 409 and r.json()["detail"] == "quote_already_linked"
    assert (await client.post(f"/v1/unit-quotes/{qid}/link", headers=h, json={"job_id": str(uuid.uuid4())})).status_code == 404
    assert (await client.post(f"/v1/unit-quotes/{uuid.uuid4()}/link", headers=h, json={"job_id": str(ok_job)})).status_code == 404
    other = await _login(client, "uq-link-other@example.com")
    assert (await client.post(f"/v1/unit-quotes/{qid}/link", headers=other, json={"job_id": str(ok_job)})).status_code == 404


async def test_expired_quote_cannot_be_linked(client, units_on):
    import datetime as dt

    from app.db.base import utcnow
    from app.models.unit_quotes import UnitQuote

    h = await _login(client, "uq-exp@example.com")
    uid = await get_user_id("uq-exp@example.com")
    qid = (await client.post("/v1/unit-quotes", headers=h, json=_body(await _kit(), max_cost="5.00"))).json()["quote_id"]
    async with get_sessionmaker()() as db:
        (await db.get(UnitQuote, uuid.UUID(qid))).expires_at = utcnow() - dt.timedelta(seconds=1)
        await db.commit()
    r = await client.post(f"/v1/unit-quotes/{qid}/link", headers=h, json={"job_id": str(await _job(uid))})
    assert r.status_code == 409 and r.json()["detail"] == "quote_expired"


# ---- learning ---------------------------------------------------------------------------------

async def _learn(uid, ref_slug, n, *, units=10, seconds):
    ids = [await _job(uid, kit=(ref_slug, "1.0.0"), units=units, seconds=seconds) for _ in range(n)]  # own sessions first
    async with get_sessionmaker()() as db:
        for jid in ids:
            assert await uq.record_actual_for_job(db, await db.get(Job, jid)) is not None
        await db.commit()


async def test_record_actual_changes_later_estimates_within_bounds(client, units_on):
    h = await _login(client, "uq-learn@example.com")
    uid = await get_user_id("uq-learn@example.com")
    ref = await _kit("uq-learn-kit")
    before = (await client.post("/v1/unit-quotes", headers=h, json=_body(ref))).json()["quote"]
    assert before["learned_samples"] == 0 and before["learned_ratio"] == "1.0000"

    # Estimate was 30 s/unit (300 s for 10 units); five jobs actually took 45 s/unit.
    await _learn(uid, "uq-learn-kit", 5, seconds=450)
    after = (await client.post("/v1/unit-quotes", headers=h, json=_body(ref))).json()
    assert after["quote"]["learned_samples"] == 5 and after["confidence"] == "medium"
    assert after["quote"]["learned_ratio"] == "1.5000"
    assert D(after["quote"]["estimated_gpu_seconds"]) == D("4500.00") > D(before["estimated_gpu_seconds"])
    assert D(after["quote"]["gpu"]["expected"]) > D(before["gpu"]["expected"])
    assert D(after["quote"]["gpu"]["expected"]) <= D(after["max_cost"])  # still clamped to the cap


async def test_learned_ratio_is_bounded_and_fewer_than_min_samples_do_not_learn(client, units_on):
    h = await _login(client, "uq-bound@example.com")
    uid = await get_user_id("uq-bound@example.com")
    ref = await _kit("uq-bound-kit")
    await _learn(uid, "uq-bound-kit", int(settings.UNIT_QUOTE_MIN_SAMPLES) - 1, seconds=450)
    few = (await client.post("/v1/unit-quotes", headers=h, json=_body(ref))).json()["quote"]
    assert few["learned_ratio"] == "1.0000" and few["confidence"] == "low"

    # Runaway actuals (5x the hint) push the ratio to the configured ceiling, never beyond.
    await _learn(uid, "uq-bound-kit", 6, seconds=1500)
    many = (await client.post("/v1/unit-quotes", headers=h, json=_body(ref))).json()["quote"]
    assert D(many["learned_ratio"]) <= D(settings.UNIT_QUOTE_RATIO_MAX)
    assert D(many["learned_ratio"]) >= D(settings.UNIT_QUOTE_RATIO_MIN)
    assert many["learned_samples"] >= int(settings.UNIT_QUOTE_MIN_SAMPLES)


async def test_record_actual_is_off_when_disabled_or_data_missing(client, monkeypatch):
    monkeypatch.setattr(settings, "PRICING_UNITS_ENABLED", False)
    await register_verify_login(client, "uq-noop@example.com")
    uid = await get_user_id("uq-noop@example.com")
    await _kit("uq-noop-kit")
    jid = await _job(uid, kit=("uq-noop-kit", "1.0.0"), units=10, seconds=100)
    plain = await _job(uid)  # not a Kit job
    nodur = await _job(uid, kit=("uq-noop-kit", "1.0.0"), units=10, seconds=0)
    wild = await _job(uid, kit=("uq-noop-kit", "1.0.0"), units=1, seconds=100000)  # > 100x the estimate
    async with get_sessionmaker()() as db:
        assert await uq.record_actual_for_job(db, await db.get(Job, jid)) is None
        monkeypatch.setattr(settings, "PRICING_UNITS_ENABLED", True)
        assert await uq.record_actual_for_job(db, await db.get(Job, plain)) is None
        assert await uq.record_actual_for_job(db, await db.get(Job, nodur)) is None
        assert await uq.record_actual_for_job(db, await db.get(Job, wild)) is None  # implausible: dropped, not learned
        assert (await db.execute(select(UnitQuoteSample))).first() is None
