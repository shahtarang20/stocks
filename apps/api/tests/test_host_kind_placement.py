"""Dedicated vs shared hosts: the label (marketplace), the customer's placement choice, and the job-page note."""

from __future__ import annotations

import types
import uuid

import pytest

from app.db.session import get_sessionmaker
from app.models.provider import ProviderMachine
from app.services.capability_flags import job_risk_note, machine_host_kind
from tests.helpers import auth_headers
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (  # noqa: F401  (_artifact_tmp is an autouse fixture)
    _artifact_tmp,
    _customer,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


async def _declare(machine_id: str, **health) -> None:
    async with get_sessionmaker()() as s:
        m = await s.get(ProviderMachine, uuid.UUID(machine_id))
        m.last_health = {**(m.last_health or {}), **health}
        await s.commit()


def _machine(health):
    return types.SimpleNamespace(last_health=health)


# ---- the label ---------------------------------------------------------------------------------------------------
def test_host_kind_is_what_the_host_declared_else_unspecified():
    assert machine_host_kind(_machine({"host_kind": "dedicated"})) == "dedicated"
    assert machine_host_kind(_machine({"host_kind": "shared"})) == "shared"
    assert machine_host_kind(_machine({})) == "unspecified"
    assert machine_host_kind(_machine(None)) == "unspecified"
    assert machine_host_kind(_machine({"host_kind": "totally-dedicated-trust-me"})) == "unspecified"
    assert machine_host_kind(_machine({"host_kind": 7})) == "unspecified"


def test_a_windows_host_is_shared_even_when_it_claims_dedicated():
    health = {"host_kind": "dedicated", "platform": {"os": "windows"}, "capabilities": ["container_tier"]}
    assert machine_host_kind(_machine(health)) == "shared"


# ---- marketplace -------------------------------------------------------------------------------------------------
async def test_marketplace_shows_and_filters_by_host_kind(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-hk1a@x.com", hostname="hk1a")
    b = await create_listable_machine(client, "p-hk1b@x.com", hostname="hk1b")
    await _declare(a["machine_id"], host_kind="dedicated")
    await _declare(b["machine_id"], host_kind="shared")

    everything = (await client.get("/v1/marketplace/machines")).json()["items"]
    kinds = {m["machine_id"]: m["host_kind"] for m in everything}
    assert kinds[a["machine_id"]] == "dedicated" and kinds[b["machine_id"]] == "shared"

    only_dedicated = (await client.get("/v1/marketplace/machines", params={"host_kind": "dedicated"})).json()["items"]
    assert [m["machine_id"] for m in only_dedicated] == [a["machine_id"]]
    assert (await client.get("/v1/marketplace/machines", params={"host_kind": "bogus"})).status_code == 422


# ---- the customer's choice ---------------------------------------------------------------------------------------
async def test_a_job_that_requires_a_dedicated_host_waits_for_one(client):
    await seed_echo_model()
    shared = await create_listable_machine(client, "p-hk2a@x.com", hostname="hk2a")
    cust = await _customer(client, "c-hk2@x.com")
    await _declare(shared["machine_id"], host_kind="shared")

    waits = await submit_job(client, cust, data_policy={"require_dedicated_host": True})
    assert waits.status_code == 201, waits.text
    assert waits.json()["status"] == "queued" and waits.json()["machine_id"] is None

    # Without the requirement the same shared host is fine.
    ok = await submit_job(client, cust)
    assert ok.json()["status"] == "reserved" and ok.json()["machine_id"] == shared["machine_id"]


async def test_a_dedicated_host_receives_a_job_that_requires_one(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-hk3@x.com", hostname="hk3")
    cust = await _customer(client, "c-hk3@x.com")
    await _declare(m["machine_id"], host_kind="dedicated")
    res = await submit_job(client, cust, data_policy={"require_dedicated_host": True})
    assert res.json()["status"] == "reserved" and res.json()["machine_id"] == m["machine_id"]


async def test_an_undeclared_host_is_not_treated_as_dedicated(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-hk4@x.com", hostname="hk4")  # never declared a kind
    cust = await _customer(client, "c-hk4@x.com")
    res = await submit_job(client, cust, data_policy={"require_dedicated_host": True})
    assert res.json()["status"] == "queued"


# ---- the job page ------------------------------------------------------------------------------------------------
async def test_job_detail_tells_the_customer_when_the_host_is_a_shared_pc(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-hk5@x.com", hostname="hk5")
    cust = await _customer(client, "c-hk5@x.com")
    await _declare(m["machine_id"], host_kind="shared")
    job_id = (await submit_job(client, cust)).json()["job_id"]

    detail = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert detail["host_kind"] == "shared"
    assert "shared PC" in detail["risk_note"] and "never charged" in detail["risk_note"]


async def test_job_detail_has_no_warning_on_a_dedicated_host(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-hk6@x.com", hostname="hk6")
    cust = await _customer(client, "c-hk6@x.com")
    await _declare(m["machine_id"], host_kind="dedicated")
    job_id = (await submit_job(client, cust)).json()["job_id"]
    detail = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert detail["host_kind"] == "dedicated" and detail["risk_note"] is None


def test_risk_note_mentions_a_rerun_and_says_nothing_when_there_is_nothing_to_say():
    assert job_risk_note("dedicated", "running", 0) is None
    assert job_risk_note("shared", "completed", 0) is None  # finished: nothing to warn about
    rerun = job_risk_note("dedicated", "running", 1)
    assert rerun and "re-run on another host" in rerun and "not charged" in rerun
    both = job_risk_note("shared", "running", 1)
    assert "re-run" in both and "shared PC" in both
