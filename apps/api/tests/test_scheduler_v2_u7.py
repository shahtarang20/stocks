"""U7: Scheduler v2 and the v2 submit path (ADR-0006 U7.1-U7.4). Written, not run by the author."""

from __future__ import annotations

import datetime as dt
import decimal
from types import SimpleNamespace as NS

import pytest

from app.services import scheduler_v2 as s2
from tests.helpers import auth_headers, register_verify_login
from tests.test_kits_u1 import _admin, _manifest, _publish, _spec_body, signer  # noqa: F401


def _job(**res):
    eff = {"gpu_count": 1, "min_vram_gb": 16, "cpu": 4, "ram_gb": 16, "disk_gb": 50, **res}
    return NS(spec_v2={"effective_resources": eff}, gpu_count=eff["gpu_count"], kit_id="blender-render", kit_version="4.2.0")


def _machine(gpus=1, vram_gb=24, cpu=16, ram_gb=64, disk=500, raw=None, caches=None, price=1, created=0):
    hw = NS(gpu_count=gpus, vram_mb=vram_gb * 1024, cpu_cores=cpu, ram_mb=ram_gb * 1024, disk_gb=disk, raw=raw)
    return NS(hardware=hw, last_health={"kit_caches": caches} if caches is not None else {}, created_at=created, price=price)


def test_vram_gate_u71():
    assert s2.machine_fit_reason(_job(), _machine(vram_gb=12)) == "vram"
    assert s2.machine_fit_reason(_job(), _machine(vram_gb=24)) is None
    assert s2.machine_fit_reason(_job(), _machine(vram_gb=0)) == "vram"  # unknown fails closed


def test_gpu_count_one_host_u73():
    j = _job(gpu_count=4)
    assert s2.machine_fit_reason(j, _machine(gpus=2)) == "gpus"
    assert s2.machine_fit_reason(j, _machine(gpus=4)) is None
    assert s2.machine_fit_reason(j, _machine(gpus=0)) == "no_gpu"


def test_compute_capability_and_cpu_ram_disk():
    j = _job(min_compute_capability="8.0")
    assert s2.machine_fit_reason(j, _machine()) == "compute_capability"
    assert s2.machine_fit_reason(j, _machine(raw={"compute_capability": "8.6"})) is None
    assert s2.machine_fit_reason(j, _machine(raw={"compute_capability": "7.5"})) == "compute_capability"
    assert s2.machine_fit_reason(_job(), _machine(cpu=2)) == "cpu"
    assert s2.machine_fit_reason(_job(), _machine(ram_gb=8)) == "ram"
    assert s2.machine_fit_reason(_job(), _machine(disk=10)) == "disk"


def test_cache_affinity_u72():
    j = _job()
    cold, warm = _machine(price=1, created=1), _machine(price=1, created=2, caches=["blender-render@4.2.0"])
    scored = [(NS(machine=cold, price=decimal.Decimal(1)), 0.5, {}), (NS(machine=warm, price=decimal.Decimal(1)), 0.5, {})]
    assert s2.rerank(j, scored)[0][0].machine is warm
    # A big score gap beats the small bonus, and the cold host is still usable alone.
    far = [(NS(machine=cold, price=decimal.Decimal(1)), 0.9, {}), (NS(machine=warm, price=decimal.Decimal(1)), 0.5, {})]
    assert s2.rerank(j, far)[0][0].machine is cold
    assert s2.rerank(j, scored[:1])[0][2]["kit_cache"] == "cold"


def test_cache_report_sanitised_and_capped():
    assert s2.sanitize_kit_caches(None) is None
    assert s2.sanitize_kit_caches("x") == []
    got = s2.sanitize_kit_caches(["ok@1.0", "ok@1.0", "Bad Ref", 5, "a" * 200 + "@1"] + [f"k{i}@1" for i in range(100)])
    assert got[0] == "ok@1.0" and len(got) == 64 and "Bad Ref" not in got


def test_estimate_minutes():
    m = {"billing_hints": {"unit": "frame", "est_gpu_seconds_per_unit": "30"}}
    assert s2.estimate_minutes(m, 10, 1) == 5
    assert s2.estimate_minutes(m, None, 1) == 1


@pytest.mark.asyncio
async def test_heartbeat_schema_drops_bad_cache_entries():
    from app.schemas.provider import HeartbeatHealth

    h = HeartbeatHealth.model_validate({"kit_caches": ["ok@1", "../evil", 3]})
    assert h.kit_caches == ["ok@1"]


@pytest.mark.asyncio
async def test_v2_submit_validation_and_privacy_refusal(client, signer):  # noqa: F811
    admin = await _admin(client)
    assert (await _publish(client, admin, signer)).status_code == 201
    t = (await register_verify_login(client, "u7-cust@example.com"))["access_token"]
    h = auth_headers(t)
    r = await client.post("/v1/jobs/v2", headers=h, json={"spec": _spec_body(params={"frames": 0})})
    assert r.status_code == 422 and r.json()["detail"]["errors"]
    r = await client.post("/v1/jobs/v2", headers=h, json={"spec": _spec_body(), "isolation_tier": "sealed"})
    assert r.status_code == 422 and r.json()["detail"] == "privacy_tier_not_supported_for_kits"
    r = await client.post("/v1/jobs/v2", headers=h, json={"spec": _spec_body(), "extra": 1})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_v2_submit_without_funds_or_machine_stores_spec(client, signer):  # noqa: F811
    admin = await _admin(client)
    assert (await _publish(client, admin, signer)).status_code == 201
    t = (await register_verify_login(client, "u7-cust2@example.com"))["access_token"]
    h = auth_headers(t)
    # An input must name one of the customer's own data-plane files (size and hash come from the database).
    import hashlib
    import uuid as _uuid

    from app.db.session import get_sessionmaker
    from app.models.data_plane import OBJECT_READY, DataFile, DataObject
    from tests.helpers import get_user_id

    uid = await get_user_id("u7-cust2@example.com")
    digest = hashlib.sha256(b"scene").hexdigest()
    async with get_sessionmaker()() as db:
        obj = DataObject(scope_key=str(uid), sha256=digest, size_bytes=5, storage_key=f"obj/{_uuid.uuid4()}", refcount=1,
                         status=OBJECT_READY)
        db.add(obj)
        await db.flush()
        f = DataFile(owner_id=uid, object_id=obj.id, name="scene.blend", size_bytes=5, sha256=digest)
        db.add(f)
        await db.commit()
        file_id = f.id
    spec = _spec_body(inputs=[{"name": "scene", "source": f"dataset:{file_id}"}])
    r = await client.post("/v1/jobs/v2", headers=h, json={"spec": spec})
    # No machine exists: the job stays QUEUED (no_provider is decided by the sweeper's retry rule).
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "queued" and body["kit"] == "blender-render@4.2.0"
    assert body["spec_v2"]["effective_resources"]["gpu_count"] == 1
    # v1 submit path is unchanged for a v1 job (unknown model still refused as before).
    r1 = await client.post("/v1/jobs", headers=h, json={"workload_type": "text-generation", "model": "nope", "input": {"prompt": "x"}})
    assert r1.status_code == 422 and r1.json()["detail"] == "model_not_supported"
