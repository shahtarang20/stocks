"""'Dedicated computers only' for Kit and v2 jobs (it already worked for plain jobs)."""

from __future__ import annotations

import types

import pytest

from app.api.routers import fanout as fanout_router
from app.core.config import settings
from app.services import fanout as fanout_svc
from app.services import scheduler_v2 as s2
from tests.helpers import auth_headers, register_verify_login
from tests.test_cpu_only_kits_and_kit_shadow import _cpu_manifest
from tests.test_kits_u1 import _admin, _publish, _spec_body, signer  # noqa: F401
from tests.test_scheduler_v2_u7 import _job as v2_job
from tests.test_scheduler_v2_u7 import _machine as v2_machine


# ---- a Kit job can ask for it, and it is stored where the scheduler reads it ----------------------------------
async def _submit(client, signer, monkeypatch, email, **extra):  # noqa: F811
    monkeypatch.setattr(settings, "CPU_ONLY_KITS_ENABLED", True)
    admin = await _admin(client)
    assert (await _publish(client, admin, signer, _cpu_manifest())).status_code == 201
    token = (await register_verify_login(client, email))["access_token"]
    return await client.post(
        "/v1/jobs/v2", headers=auth_headers(token), json={"spec": _spec_body(inputs=[]), **extra}
    )


@pytest.mark.asyncio
async def test_a_kit_job_can_require_dedicated_computers(client, signer, monkeypatch):  # noqa: F811
    res = await _submit(client, signer, monkeypatch, "ded-kit-1@example.com", require_dedicated_host=True)
    assert res.status_code == 201, res.text
    policy = res.json()["data_policy"]
    assert policy["require_dedicated_host"] is True
    assert "v2_plan" in policy  # the plan is still there next to it


@pytest.mark.asyncio
async def test_a_kit_job_does_not_require_it_unless_asked(client, signer, monkeypatch):  # noqa: F811
    res = await _submit(client, signer, monkeypatch, "ded-kit-2@example.com")
    assert res.status_code == 201, res.text
    assert "require_dedicated_host" not in res.json()["data_policy"]
    res = await client.post(
        "/v1/jobs/v2",
        headers=auth_headers((await register_verify_login(client, "ded-kit-3@example.com"))["access_token"]),
        json={"spec": _spec_body(inputs=[]), "require_dedicated_host": False},
    )
    assert res.status_code == 201, res.text
    assert "require_dedicated_host" not in res.json()["data_policy"]


@pytest.mark.asyncio
async def test_the_field_must_be_a_real_boolean(client, signer, monkeypatch):  # noqa: F811
    res = await _submit(client, signer, monkeypatch, "ded-kit-4@example.com", require_dedicated_host="maybe")
    assert res.status_code == 422


# ---- the v2 scheduler gate honours it --------------------------------------------------------------------------
def _with_policy(job, **policy):
    job.data_policy = policy
    return job


def test_the_v2_gate_refuses_shared_and_undeclared_hosts_and_accepts_a_dedicated_one():
    job = _with_policy(v2_job(), require_dedicated_host=True)
    for health in ({"host_kind": "shared"}, {}, {"host_kind": "totally-dedicated"}):
        machine = v2_machine()
        machine.last_health = health
        assert s2.machine_fit_reason(job, machine) == "host_not_dedicated", health
    ok = v2_machine()
    ok.last_health = {"host_kind": "dedicated"}
    assert s2.machine_fit_reason(job, ok) is None


def test_without_the_requirement_a_shared_host_is_fine():
    machine = v2_machine()
    machine.last_health = {"host_kind": "shared"}
    assert s2.machine_fit_reason(v2_job(), machine) is None


# ---- fan-out: the parent records it, every part and straggler copy honours it -----------------------------------
def _parent(**policy):
    return types.SimpleNamespace(data_policy=policy)


_PLAN = {"kit": "upscale@1.0.0", "outputs": [{"name": "frames", "path": "frames"}], "inputs": [{"name": "a"}]}


def test_every_fanout_part_inherits_the_requirement():
    policy = fanout_svc.part_data_policy(_parent(fanout=True, require_dedicated_host=True), _PLAN, None)
    assert policy["require_dedicated_host"] is True and policy["v2_plan"] == _PLAN


def test_fanout_parts_do_not_get_it_unless_the_parent_has_it():
    for parent in (_parent(fanout=True), _parent(), types.SimpleNamespace(data_policy=None)):
        assert "require_dedicated_host" not in fanout_svc.part_data_policy(parent, _PLAN, None)


def test_private_fanout_parts_keep_their_minimal_policy_and_still_honour_the_requirement():
    keys = {"result_public_key": "ab" * 32}
    policy = fanout_svc.part_data_policy(_parent(fanout=True, require_dedicated_host=True), _PLAN, keys)
    assert policy["private_fanout"] is True and policy["result_public_key"] == "ab" * 32
    assert policy["v2_plan"] == {"kit": "upscale@1.0.0", "outputs": _PLAN["outputs"]}  # no inputs, no assets, no egress
    assert policy["require_dedicated_host"] is True


@pytest.mark.asyncio
async def test_the_fanout_route_passes_the_requirement_to_the_service(client, signer, monkeypatch):  # noqa: F811
    seen = {}

    async def fake_submit(db, **kwargs):
        seen.update(kwargs)
        raise fanout_svc.FanoutError("stop_here", 422, [])

    monkeypatch.setattr(fanout_router.dp, "require_enabled", lambda: None)
    monkeypatch.setattr(fanout_router.svc, "submit_fanout", fake_submit)
    token = (await register_verify_login(client, "ded-fan@example.com"))["access_token"]
    res = await client.post(
        "/v1/jobs/v2/fanout", headers=auth_headers(token), json={"spec": _spec_body(), "require_dedicated_host": True}
    )
    assert res.status_code == 422
    assert seen["require_dedicated_host"] is True
