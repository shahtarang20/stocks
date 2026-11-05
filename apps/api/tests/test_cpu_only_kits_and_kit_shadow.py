"""CPU-only Kits (gpu_count 0) and Kit shadow / reference runs. WRITTEN, NOT RUN by the author.

Bug re-insertion list: make ``stored_gpu_count`` return 1 for gpu_count 0 while the setting is off (refusal test fails);
drop ``quote_price_per_hour`` from ``_create_quote_for_job`` (quote fraction test fails); return ``job.gpu_count`` from
``_claim_gpu_count`` for a CPU-only job (claim test fails); stop calling ``kit_shadow_data_policy`` in ``create_shadow_job``
(plan / checkpoint tests fail); remove the ``is_unbilled_job`` guard from ``maybe_schedule_kit_reference`` (no-resample test fails).
"""

from __future__ import annotations

import copy
import decimal
import uuid
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.billing import Invoice, LedgerTransaction
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.services import billing
from app.services import job_fsm as fsm
from app.services import kit_verification_run
from app.services import scheduler as sched
from app.services import scheduler_v2 as s2
from app.services import verification as verification_service
from tests.helpers import auth_headers, get_user_id, register_verify_login
from tests.test_kits_u1 import _admin, _manifest, _publish, _spec_body, signer  # noqa: F401
from tests.test_phase3_marketplace import create_listable_machine


def _cpu_job(gpu_count=0, **res):
    eff = {"gpu_count": gpu_count, "min_vram_gb": 0, "cpu": 2, "ram_gb": 4, "disk_gb": 10, **res}
    # The stored gpu_count is the effective count, except a CPU-only job which stores one billing slot.
    return NS(spec_v2={"effective_resources": eff}, gpu_count=max(1, gpu_count), kit_id="scientific-compute", kit_version="1.0.0")


def _machine(*, gpus=0, cpu=8, ram_gb=32, disk=100, health=None):
    hw = NS(gpu_count=gpus, vram_mb=0, cpu_cores=cpu, ram_mb=ram_gb * 1024, disk_gb=disk, raw={})
    return NS(hardware=hw, last_health=health if health is not None else {})


# ---------------------------------------------------------------------------
# helpers in scheduler_v2
# ---------------------------------------------------------------------------
def test_is_cpu_only_job_only_for_an_explicit_zero():
    assert s2.is_cpu_only_job(_cpu_job(0)) is True
    assert s2.is_cpu_only_job(_cpu_job(1)) is False
    assert s2.is_cpu_only_job(NS(spec_v2={"effective_resources": {}}, gpu_count=1)) is False  # missing is not CPU-only
    assert s2.is_cpu_only_job(NS(spec_v2={"effective_resources": {"gpu_count": None}}, gpu_count=1)) is False
    assert s2.is_cpu_only_job(NS(spec_v2=None, gpu_count=0)) is False  # a v1 job never takes this path


def test_stored_gpu_count_refuses_zero_unless_enabled(monkeypatch):
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", False)
    with pytest.raises(s2.CpuOnlyKitsDisabled):
        s2.stored_gpu_count({"gpu_count": 0})
    assert s2.stored_gpu_count({"gpu_count": 2}) == 2
    assert s2.stored_gpu_count({"gpu_count": None}) == 1  # unchanged legacy default
    assert s2.stored_gpu_count({}) == 1
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", True)
    assert s2.stored_gpu_count({"gpu_count": 0}) == 1  # one scheduler/billing slot, priced by the fraction


def test_runtime_gpu_count_is_zero_for_cpu_only_and_unchanged_otherwise():
    assert s2.runtime_gpu_count(_cpu_job(0)) == 0
    assert s2.runtime_gpu_count(_cpu_job(1)) == 1
    from app.api.routers.agents import _claim_gpu_count

    assert _claim_gpu_count(_cpu_job(0)) == 0  # the agent must not add --gpus
    assert _claim_gpu_count(NS(spec_v2=None, gpu_count=2)) == 2  # v1 claim unchanged
    assert _claim_gpu_count(_cpu_job(2)) == 2


def test_machine_fit_for_cpu_only_job(monkeypatch):
    job = _cpu_job(0)
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", False)
    assert s2.machine_fit_reason(job, _machine(health={"cpu_capable": True})) == "cpu_only_disabled"
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", True)
    assert s2.machine_fit_reason(job, _machine(health={})) == "not_cpu_capable"
    assert s2.machine_fit_reason(job, _machine(health={"cpu_capable": "yes"})) == "not_cpu_capable"  # only a real boolean
    assert s2.machine_fit_reason(job, _machine(health=None)) == "not_cpu_capable"
    # No GPU is needed, and a GPU host that opted in is fine too.
    assert s2.machine_fit_reason(job, _machine(gpus=0, health={"cpu_capable": True})) is None
    assert s2.machine_fit_reason(job, _machine(gpus=4, health={"cpu_capable": True})) is None
    # CPU, RAM and disk are still enforced.
    assert s2.machine_fit_reason(job, _machine(cpu=1, health={"cpu_capable": True})) == "cpu"
    assert s2.machine_fit_reason(job, _machine(ram_gb=2, health={"cpu_capable": True})) == "ram"
    assert s2.machine_fit_reason(job, _machine(disk=5, health={"cpu_capable": True})) == "disk"


def test_gpu_jobs_are_not_affected_by_the_cpu_flag(monkeypatch):
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", True)
    gpu_job = _cpu_job(1)
    assert s2.machine_fit_reason(gpu_job, _machine(gpus=0, health={"cpu_capable": True})) == "no_gpu"


def test_rate_fraction_never_zero_and_rounded_to_cents(monkeypatch):
    job = _cpu_job(0)
    monkeypatch.setattr(settings, "CPU_ONLY_RATE_FRACTION", 0.1)
    assert s2.quote_price_per_hour(job, decimal.Decimal("1.50")) == decimal.Decimal("0.15")
    assert s2.quote_price_per_hour(_cpu_job(1), decimal.Decimal("1.50")) == decimal.Decimal("1.50")  # GPU jobs untouched
    assert s2.quote_price_per_hour(job, decimal.Decimal("0.02")) == decimal.Decimal("0.01")  # cent floor, never free
    for bad in (0, -1, 2, float("nan"), "x"):
        monkeypatch.setattr(settings, "CPU_ONLY_RATE_FRACTION", bad)
        assert s2.cpu_rate_fraction() == decimal.Decimal("0.1")
    monkeypatch.setattr(settings, "CPU_ONLY_RATE_FRACTION", 1)
    assert s2.quote_price_per_hour(job, decimal.Decimal("2.00")) == decimal.Decimal("2.00")


def test_settings_defaults():
    assert settings.CPU_ONLY_KITS_ENABLED is False
    assert settings.CPU_ONLY_RATE_FRACTION == 0.1


def test_heartbeat_health_keeps_only_a_real_boolean_cpu_capable():
    from app.schemas.provider import HeartbeatHealth

    assert HeartbeatHealth.model_validate({"cpu_capable": True}).cpu_capable is True
    assert HeartbeatHealth.model_validate({"cpu_capable": "true"}).cpu_capable is None
    assert HeartbeatHealth.model_validate({"cpu_capable": 1}).cpu_capable is None
    assert HeartbeatHealth.model_validate({}).cpu_capable is None


# ---------------------------------------------------------------------------
# quote path: the fraction is applied there, nowhere in billing
# ---------------------------------------------------------------------------
class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_quote_applies_fraction_only_for_cpu_only_jobs(monkeypatch):
    async def _fee(_db):
        return decimal.Decimal("0.10")

    monkeypatch.setattr(sched, "get_active_fee_rate", _fee)
    monkeypatch.setattr(settings, "CPU_ONLY_RATE_FRACTION", 0.1)
    machine = NS(id=uuid.uuid4(), asking_price_per_hour=decimal.Decimal("2.00"))
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

    def _job(gpu_count_eff, stored):
        eff = {"gpu_count": gpu_count_eff}
        return NS(customer_id=uuid.uuid4(), workload_type="kit", gpu_count=stored, estimated_minutes=60,
                  spec_v2={"effective_resources": eff})

    cpu_q = await sched._create_quote_for_job(_FakeDb(), _job(0, 1), machine, now)
    assert cpu_q.price_per_hour_snapshot == decimal.Decimal("0.20")
    assert cpu_q.gpu_count == 1 and cpu_q.gpu_cost == decimal.Decimal("0.20")
    gpu_q = await sched._create_quote_for_job(_FakeDb(), _job(1, 1), machine, now)
    assert gpu_q.price_per_hour_snapshot == decimal.Decimal("2.00") and gpu_q.gpu_cost == decimal.Decimal("2.00")
    v1 = NS(customer_id=uuid.uuid4(), workload_type="text-generation", gpu_count=1, estimated_minutes=60, spec_v2=None)
    assert (await sched._create_quote_for_job(_FakeDb(), v1, machine, now)).price_per_hour_snapshot == decimal.Decimal("2.00")


# ---------------------------------------------------------------------------
# submit path
# ---------------------------------------------------------------------------
def _cpu_manifest():
    return _manifest(resource_defaults={"gpu_count": 0, "cpu": 1, "ram_gb": 1, "disk_gb": 1}, inputs=[],
                     billing_hints={"unit": "frame", "est_gpu_seconds_per_unit": "30"})


@pytest.mark.asyncio
async def test_cpu_only_submit_refused_when_disabled(client, signer, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", False)
    admin = await _admin(client)
    assert (await _publish(client, admin, signer, _cpu_manifest())).status_code == 201  # publishing is not gated
    t = (await register_verify_login(client, "cpu-off@example.com"))["access_token"]
    r = await client.post("/v1/jobs/v2", headers=auth_headers(t), json={"spec": _spec_body(inputs=[])})
    assert r.status_code == 422, r.text
    errs = r.json()["detail"]["errors"]
    assert errs[0]["code"] == "cpu_only_kits_disabled"


@pytest.mark.asyncio
async def test_cpu_only_submit_when_enabled_stores_one_slot_and_zero_effective_gpus(client, signer, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", True)
    admin = await _admin(client)
    assert (await _publish(client, admin, signer, _cpu_manifest())).status_code == 201
    t = (await register_verify_login(client, "cpu-on@example.com"))["access_token"]
    r = await client.post("/v1/jobs/v2", headers=auth_headers(t), json={"spec": _spec_body(inputs=[])})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["gpu_count"] == 1  # the billing slot
    assert body["spec_v2"]["effective_resources"]["gpu_count"] == 0  # what the agent and the scheduler see
    assert body["status"] == "queued"  # no cpu-capable host yet


# ---------------------------------------------------------------------------
# shadow / reference runs of Kit jobs
# ---------------------------------------------------------------------------
PLAN = {
    "kit": {"slug": "blender-render", "version": "4.2.0"},
    "inputs": [{"name": "scene", "file_id": "f1", "sha256": "a" * 64, "size_bytes": 3, "cacheable": False}],
    "assets": [],
    "outputs": [{"name": "frames", "path": "/out/frames/*.png", "required": True}],
    "egress": [],
    "checkpoint": {"dir": "/ckpt", "interval_seconds": 60, "resume": {"file_id": "old"}},
}
# A plan carrying a ``service`` block IS a Service replica (is_service_replica_job) and is never shadowed, so the primary
# here must not have one; kit_shadow_data_policy dropping ``service`` is covered directly in the unit test below.


async def _kit_primary(client, tag, *, cpu_only=False):
    m1 = await create_listable_machine(client, f"kp1-{tag}@x.com", hostname=f"k1{tag}")
    m2 = await create_listable_machine(client, f"kp2-{tag}@x.com", hostname=f"k2{tag}")
    await register_verify_login(client, f"kc-{tag}@x.com")
    customer_id = await get_user_id(f"kc-{tag}@x.com")
    eff = {"gpu_count": 0 if cpu_only else 1}
    sm = get_sessionmaker()
    async with sm() as s:
        primary = Job(
            customer_id=customer_id, workload_type="kit", model="blender-render@4.2.0", input={},
            status=fsm.COMPLETED, gpu_count=1, estimated_minutes=2, max_runtime_seconds=600, execution_mode="hardware",
            isolation_tier="container", machine_id=uuid.UUID(m1["machine_id"]),
            spec_v2={"spec": {"params": {"frames": 2}}, "effective_resources": eff, "kit_image": "r/x@sha256:" + "a" * 64,
                     "kit_manifest_sha256": "b" * 64},
            kit_id="blender-render", kit_version="4.2.0", network_policy={"egress": ["example.org:443"]},
            data_policy={"v2_plan": copy.deepcopy(PLAN), "allowed_regions": ["us-east"]},
        )
        s.add(primary)
        await s.flush()
        pid = primary.id
        if cpu_only:
            for mid in (m2["machine_id"],):
                mach = await s.get(ProviderMachine, uuid.UUID(mid))
                mach.last_health = {**(mach.last_health or {}), "cpu_capable": True}
        await s.commit()
    return m1, m2, pid, customer_id


@pytest.mark.asyncio
async def test_kit_shadow_copies_spec_kit_network_and_plan_without_state(client):
    m1, m2, pid, _cid = await _kit_primary(client, "copy")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, pid)
        pair = await verification_service.create_shadow_job(s, primary)
        assert pair is not None
        shadow, check = pair
        await s.commit()
        assert shadow.shadow_of_job_id == primary.id
        assert shadow.spec_v2 == primary.spec_v2 and shadow.spec_v2 is not primary.spec_v2
        assert (shadow.kit_id, shadow.kit_version) == ("blender-render", "4.2.0")
        assert shadow.network_policy == {"egress": ["example.org:443"]}
        plan = shadow.data_policy["v2_plan"]
        assert plan["kit"] == PLAN["kit"] and plan["inputs"] == PLAN["inputs"]
        assert plan["outputs"] == PLAN["outputs"] and plan["egress"] == PLAN["egress"]
        assert "checkpoint" not in plan and "service" not in plan  # no resume, never a replica
        assert shadow.data_policy["allowed_regions"] == ["us-east"]  # placement policy is kept
        # The primary's own plan is untouched (deep copy, not an alias).
        assert primary.data_policy["v2_plan"]["checkpoint"]["resume"] == {"file_id": "old"}
        # Different machine than the primary, reserved and pinned for the claim.
        assert str(shadow.machine_id) == m2["machine_id"] != m1["machine_id"]
        assert shadow.status == fsm.RESERVED


@pytest.mark.asyncio
async def test_kit_shadow_claim_has_no_checkpoint_and_outputs_go_to_its_own_job(client):
    from app.api.routers.agent_v2 import build_claim_v2

    _m1, _m2, pid, _cid = await _kit_primary(client, "claim")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, pid)
        shadow, _check = await verification_service.create_shadow_job(s, primary)
        claim = await build_claim_v2(s, shadow)
        await s.commit()
        assert claim is not None and claim["spec_version"] == 2
        assert claim["checkpoint"] is None and "service" not in claim
        assert claim["kit"] == PLAN["kit"] and claim["outputs"] == PLAN["outputs"]
        # Uploads are stored under out/<uploading job id>/ (agent_v2.start_upload), so the shadow can never write into
        # the primary's output prefix; kit_verification_run reads each job's own prefix.
        assert shadow.id != primary.id


@pytest.mark.asyncio
async def test_kit_shadow_is_unbilled_idempotent_and_never_resampled(client, monkeypatch):
    _m1, _m2, pid, cid = await _kit_primary(client, "bill")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, pid)
        first = await verification_service.create_shadow_job(s, primary)
        again = await verification_service.create_shadow_job(s, primary)
        assert first is not None and again is not None and again[0].id == first[0].id  # idempotent per primary
        shadow = first[0]
        sid = shadow.id
        assert verification_service.is_unbilled_job(shadow) is True
        # A shadow is never itself shadowed, by either sampler.
        assert await verification_service.create_shadow_job(s, shadow) is None
        monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 1.0)

        async def _manifest_of(_db, _job):
            return {"verification": {"method": "exact", "sample_fraction": 1}}

        monkeypatch.setattr(kit_verification_run, "_manifest_of", _manifest_of)
        assert await kit_verification_run.maybe_schedule_kit_reference(s, shadow) is None
        # reserve_for_job takes no hold for it, so nothing can be charged or released to the customer.
        assert await billing.reserve_for_job(s, shadow, NS(total=decimal.Decimal("5.00"))) is None
        await s.commit()
    async with sm() as s:
        assert await billing.get_hold_minor(s, sid) == 0
        assert (await s.execute(select(Invoice).where(Invoice.job_id == sid))).scalars().first() is None
        assert (await s.execute(select(LedgerTransaction).where(LedgerTransaction.reference_id == str(sid)))).scalars().all() == []


@pytest.mark.asyncio
async def test_maybe_schedule_kit_reference_samples_a_billed_primary(client, monkeypatch):
    _m1, m2, pid, _cid = await _kit_primary(client, "samp")
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 1.0)

    async def _manifest_of(_db, _job):
        return {"verification": {"method": "exact", "sample_fraction": 1}}

    monkeypatch.setattr(kit_verification_run, "_manifest_of", _manifest_of)
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, pid)
        pair = await kit_verification_run.maybe_schedule_kit_reference(s, primary)
        assert pair is not None and str(pair[0].machine_id) == m2["machine_id"]
        # Sampling at 0 creates nothing.
    monkeypatch.setattr(settings, "KIT_VERIFICATION_SAMPLE_RATE", 0.0)
    assert kit_verification_run.sample_rate({"verification": {"sample_fraction": 1}}) == 0.0


@pytest.mark.asyncio
async def test_cpu_only_kit_shadow_goes_only_to_another_cpu_capable_host(client, monkeypatch):
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", True)
    m1, m2, pid, _cid = await _kit_primary(client, "cpu", cpu_only=True)
    sm = get_sessionmaker()
    async with sm() as s:
        primary = await s.get(Job, pid)
        pair = await verification_service.create_shadow_job(s, primary)
        assert pair is not None
        shadow = pair[0]
        assert str(shadow.machine_id) == m2["machine_id"]  # the one cpu_capable host, not the primary's machine
        assert shadow.spec_v2["effective_resources"]["gpu_count"] == 0
        from app.api.routers.agents import _claim_gpu_count

        assert _claim_gpu_count(shadow) == 0  # the shadow's claim also starts without --gpus
        await s.commit()
    # With the setting off no host qualifies for the CPU-only shadow: it is skipped (None), not run on a GPU box.
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", False)
    m1b, m2b, pid2, _ = await _kit_primary(client, "cpu2", cpu_only=True)
    async with sm() as s:
        assert await verification_service.create_shadow_job(s, await s.get(Job, pid2)) is None


def test_kit_shadow_data_policy_helper_handles_missing_data():
    assert verification_service.kit_shadow_data_policy(None) == {}
    src = {"v2_plan": {"kit": {"slug": "k"}, "checkpoint": {"dir": "/c"}, "service": {"port": 1}}, "x": [1]}
    out = verification_service.kit_shadow_data_policy(src)
    assert out == {"v2_plan": {"kit": {"slug": "k"}}, "x": [1]}
    assert src["v2_plan"]["checkpoint"] == {"dir": "/c"}  # source untouched
    out["x"].append(2)
    assert src["x"] == [1]
