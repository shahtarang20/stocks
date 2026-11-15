"""Container rental, API side (docs/architecture/RENTAL-container-rental-design.md sections 6, 7 and 11).

Create rules and refusals, claim eligibility, the one-GPU-user rule, the customer export route, the host file routes (inputs, uploads,
export result) with their ownership and window rules, the marketplace field and filter, and that money is untouched (the existing
session settlement bills a rental).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.provider import ProviderMachine
from app.models.sessions import SESSION_ACTIVE, SESSION_PENDING, SESSION_STARTING, SESSION_STOPPED, ComputeSession
from app.services import rental as rental_svc
from app.services import sessions as svc
from tests.helpers import auth_headers, get_user_id, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import agent_headers, claim as claim_job, seed_echo_model, submit_job
from tests.test_u2_data_plane import MIB, _upload, sha, store  # noqa: F401  (store is a fixture)

pytestmark = pytest.mark.asyncio

PUBKEY = "ab" * 32
HOST_KEY = "cd" * 32
DIGEST = "b" * 64
GHCR = f"ghcr.io/acme/trainer@sha256:{DIGEST}"
TEMPLATE_IMAGE = f"ghcr.io/acme/rental-pytorch-jupyter@sha256:{'c' * 64}"


# ---- fixtures and helpers ---------------------------------------------------------------------------------------------
@pytest.fixture
def rental_on(monkeypatch, tmp_path):
    """The platform switch on, and a template list with one built template, one without a digest, one with a bad registry."""
    path = tmp_path / "templates.json"
    base = {"mode": "tcp", "tcp_port": 8888, "command": None, "gpu_required": True, "libraries": ["torch"], "description": "d"}
    path.write_text(json.dumps({"templates": [
        {**base, "slug": "pytorch-jupyter", "name": "PyTorch", "digest": TEMPLATE_IMAGE,
         "default_egress": ["pypi.org:443", "files.pythonhosted.org:443"]},
        {**base, "slug": "vision-jupyter", "name": "Vision", "digest": None, "default_egress": []},
        {**base, "slug": "evil-registry", "name": "Evil", "digest": f"evil.example.com/x@sha256:{'d' * 64}", "default_egress": []},
    ]}))
    monkeypatch.setattr(rental_svc, "TEMPLATE_FILE", path)
    monkeypatch.setattr(settings, "CONTAINER_RENTAL_ENABLED", True)
    yield path


def _body(**over):
    b = {"template": "pytorch-jupyter", "max_price_per_gpu_hour": "2.00", "max_duration_seconds": 3600, "idle_timeout_seconds": 900,
         "client_pubkey": PUBKEY, "accept_rental_terms": True}
    b.update(over)
    return b


async def _login(client, email):
    return auth_headers((await register_verify_login(client, email))["access_token"])


async def _create(client, h, **over):
    return await client.post("/v1/sessions", headers=h, json=_body(**over))


async def _machine(client, email, *, opted_in=True, hostname="rent-box"):
    m = await create_listable_machine(client, email, hostname=hostname, price="1.00")
    await _set_flags(m["machine_id"], opted_in)
    return m


async def _set_flags(machine_id, opted_in: bool, *, os_name="linux", proxy=True):
    async with get_sessionmaker()() as db:
        mach = await db.get(ProviderMachine, uuid.UUID(machine_id))
        caps = ["container_tier", "nvidia_cuda"] + (["container_rental"] if opted_in else []) + (["egress_proxy"] if proxy else [])
        mach.last_health = {"platform": {"os": os_name, "runtime_backend": "docker"}, "capabilities": caps}
        await db.commit()


async def _row(sid) -> ComputeSession:
    async with get_sessionmaker()() as db:
        return (await db.execute(select(ComputeSession).where(ComputeSession.id == uuid.UUID(str(sid))))).scalar_one()


async def _host_post(client, machine, path, body=None):
    headers, raw = agent_headers(machine["machine_id"], machine["secret"], body or {})
    return await client.post(path, content=raw, headers=headers)


async def _claim_session(client, machine):
    return await _host_post(client, machine, "/v1/agent/sessions/claim", {"host_pubkey": HOST_KEY})


async def _make_active(client, machine, sid):
    claimed = await _claim_session(client, machine)
    assert claimed.status_code == 200 and claimed.json()["session"]["id"] == str(sid), claimed.text
    started = await _host_post(client, machine, f"/v1/agent/sessions/{sid}/started")
    assert started.status_code == 200
    return claimed.json()["session"]


async def _ready_file(client, store_, email, *, name="data.bin", data=None):
    owner = await get_user_id(email)
    data = data or (b"x" * 1024)
    async with get_sessionmaker()() as db:
        _, file = await _upload(db, store_, owner, data, name=name)
        await db.commit()
        return str(file.id), data


# ---- create: the happy paths --------------------------------------------------------------------------------------------
async def test_a_template_rental_is_created_pending_with_the_templates_settings(client, rental_on):
    h = await _login(client, "rt-ok@example.com")
    r = await _create(client, h, workspace_gb=10, memory_mb=8192, cpus=2)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["rental"] is True and out["template"] == "pytorch-jupyter" and out["status"] == SESSION_PENDING
    assert out["mode"] == "tcp" and out["workspace_gb"] == 10 and out["memory_mb"] == 8192 and out["cpus"] == 2
    assert out["inputs"] == [] and out["exports"] == [] and out["export_state"] is None and out["requested_machine_id"] is None
    row = await _row(out["id"])
    assert row.image_digest == TEMPLATE_IMAGE and row.tcp_port == 8888 and row.command is None  # the image's own entrypoint runs
    assert row.network_policy == {"egress": ["files.pythonhosted.org:443", "pypi.org:443"]}
    assert row.isolation_tier == "container" and row.terms_accepted_at is not None and float(out["held"]) > 0


async def test_a_custom_image_rental_keeps_the_command_and_has_no_network_by_default(client, rental_on):
    h = await _login(client, "rt-img@example.com")
    r = await _create(client, h, template=None, image_digest=GHCR, mode="pty", command=["/bin/bash", "-l"])
    assert r.status_code == 201, r.text
    row = await _row(r.json()["id"])
    assert row.rental is True and row.image_digest == GHCR and row.command == ["/bin/bash", "-l"] and row.network_policy is None
    assert r.json()["template"] is None


async def test_defaults_for_memory_and_cpus(client, rental_on):
    h = await _login(client, "rt-defaults@example.com")
    out = (await _create(client, h)).json()
    assert out["memory_mb"] == 16384 and out["cpus"] == 4 and out["workspace_gb"] == 0


async def test_a_request_may_narrow_the_templates_network_but_not_widen_it(client, rental_on):
    h = await _login(client, "rt-egress@example.com")
    narrow = await _create(client, h, network_policy={"egress": ["pypi.org:443"]})
    assert narrow.status_code == 201
    assert (await _row(narrow.json()["id"])).network_policy == {"egress": ["pypi.org:443"]}
    wide = await _create(client, h, network_policy={"egress": ["github.com:443"]})  # allowed globally, but not by this template
    assert wide.status_code == 422 and wide.json()["detail"] == "egress_not_declared_by_template"


async def test_a_custom_image_may_ask_only_for_hosts_on_the_platform_list(client, rental_on):
    h = await _login(client, "rt-egress2@example.com")
    ok = await _create(client, h, template=None, image_digest=GHCR, mode="pty", network_policy={"egress": ["github.com:443", "pypi.org:443"]})
    assert ok.status_code == 201
    assert (await _row(ok.json()["id"])).network_policy == {"egress": ["github.com:443", "pypi.org:443"]}
    for bad in (["evil.example.com:443"], ["pypi.org:80"], ["169.254.169.254:80"], ["localhost:8080"], ["*.pypi.org:443"]):
        r = await _create(client, h, template=None, image_digest=GHCR, mode="pty", network_policy={"egress": bad})
        assert r.status_code == 422, (bad, r.text)


# ---- create: refusals -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("over,code,status", [
    ({"accept_rental_terms": False}, "rental_terms_not_accepted", 422),
    ({}, None, 201),
    ({"isolation_tier": "sealed"}, "rental_requires_container_tier", 422),
    ({"isolation_tier": "gvisor"}, "rental_requires_container_tier", 422),
    ({"kit_ref": "x@1"}, "template_excludes_kit_ref_and_image_digest", 422),
    ({"image_digest": GHCR}, "template_excludes_kit_ref_and_image_digest", 422),
    ({"template": "nope"}, "template_not_found", 404),
    ({"template": "vision-jupyter"}, "template_not_found", 404),  # no built image yet: hidden and refused
    ({"template": "evil-registry"}, "template_not_found", 404),  # its registry is not allowed
    ({"template": "../x"}, "template_not_found", 404),
    ({"workspace_gb": 101}, "rental_limit_out_of_range", 422),
    ({"workspace_gb": -1}, "rental_limit_out_of_range", 422),
    ({"memory_mb": 512}, "rental_limit_out_of_range", 422),
    ({"memory_mb": 262145}, "rental_limit_out_of_range", 422),
    ({"cpus": 0}, "rental_limit_out_of_range", 422),
    ({"cpus": 65}, "rental_limit_out_of_range", 422),
])
async def test_create_rules(client, rental_on, over, code, status):
    h = await _login(client, f"rt-rule-{uuid.uuid4().hex[:8]}@example.com")
    r = await _create(client, h, **over)
    assert r.status_code == status, r.text
    if code:
        assert r.json()["detail"] == code


@pytest.mark.parametrize("image,code", [
    (f"evil.example.com/x@sha256:{DIGEST}", "image_registry_not_allowed"),
    (f"localhost/x@sha256:{DIGEST}", "image_registry_not_allowed"),
    (f"localhost:5000/x@sha256:{DIGEST}", "image_registry_not_allowed"),
    (f"registry.example/x@sha256:{DIGEST}", "image_registry_not_allowed"),
    ("ghcr.io/acme/x:latest", "image_must_be_pinned_by_sha256_digest"),
    (f"ghcr.io/acme/x@sha256:{'B' * 64}", "image_must_be_pinned_by_sha256_digest"),
    ("ghcr.io/acme/x@sha256:abc", "image_must_be_pinned_by_sha256_digest"),
    ("", "give_exactly_one_of_kit_ref_or_image_digest"),
])
async def test_custom_image_rules(client, rental_on, image, code):
    h = await _login(client, f"rt-img-{uuid.uuid4().hex[:8]}@example.com")
    r = await _create(client, h, template=None, image_digest=image or None, mode="pty")
    assert r.status_code == 422 and r.json()["detail"] == code, r.text


async def test_docker_hub_and_the_other_allowed_registries_are_accepted(client, rental_on):
    h = await _login(client, "rt-reg@example.com")
    for image in (f"pytorch/pytorch@sha256:{DIGEST}", f"docker.io/library/python@sha256:{DIGEST}", f"nvcr.io/nvidia/pytorch@sha256:{DIGEST}",
                  f"quay.io/org/x@sha256:{DIGEST}"):
        r = await _create(client, h, template=None, image_digest=image, mode="pty")
        assert r.status_code == 201, (image, r.text)
        await client.post(f"/v1/sessions/{r.json()['id']}/stop", headers=h)


async def test_the_allowed_registry_list_is_a_setting(client, rental_on, monkeypatch):
    h = await _login(client, "rt-reg2@example.com")
    monkeypatch.setattr(settings, "RENTAL_ALLOWED_REGISTRIES", "quay.io")
    assert (await _create(client, h, template=None, image_digest=GHCR, mode="pty")).json()["detail"] == "image_registry_not_allowed"
    assert (await _create(client, h)).status_code == 404  # the template's own registry (ghcr.io) is no longer allowed: hidden


@pytest.mark.parametrize("command", [[], ["a"] * 33, ["a\x00b"], [""], [1], "bash", ["x" * 513]])
async def test_a_bad_command_is_refused(client, rental_on, command):
    h = await _login(client, f"rt-cmd-{uuid.uuid4().hex[:8]}@example.com")
    r = await _create(client, h, template=None, image_digest=GHCR, mode="pty", command=command)
    assert r.status_code == 422, r.text


async def test_rental_is_refused_when_the_platform_switch_is_off(client, rental_on, monkeypatch):
    monkeypatch.setattr(settings, "CONTAINER_RENTAL_ENABLED", False)
    h = await _login(client, "rt-off@example.com")
    assert (await _create(client, h)).json()["detail"] == "container_rental_not_enabled"
    old = await _create(client, h, template=None, image_digest=GHCR, mode="pty")  # the old gate and its old error code
    assert old.status_code == 422 and old.json()["detail"] == "custom_images_not_enabled_for_sessions"


async def test_rental_only_fields_are_refused_on_a_kit_session(client, rental_on):
    from tests.test_sessions_routes import _kit

    h = await _login(client, "rt-kitfields@example.com")
    ref = await _kit()
    base = {"kit_ref": ref, "max_price_per_gpu_hour": "0.10", "client_pubkey": PUBKEY}
    for over in ({"workspace_gb": 5}, {"memory_mb": 4096}, {"cpus": 2}, {"command": ["bash"]}, {"inputs": [{"file_id": str(uuid.uuid4()), "path": "a"}]}):
        r = await client.post("/v1/sessions", headers=h, json={**base, **over})
        assert r.status_code == 422, (over, r.text)
    assert (await client.post("/v1/sessions", headers=h, json=base)).status_code == 201  # and a plain Kit session is unchanged


# ---- create: the machine the customer picks -------------------------------------------------------------------------------
async def test_picking_a_machine_that_offers_rental(client, rental_on):
    m = await _machine(client, "rt-m1-host@example.com")
    h = await _login(client, "rt-m1@example.com")
    r = await _create(client, h, machine_id=m["machine_id"])
    assert r.status_code == 201 and r.json()["requested_machine_id"] == m["machine_id"]


async def test_picking_a_machine_that_cannot_take_it_is_refused(client, rental_on):
    h = await _login(client, "rt-m2@example.com")
    out = await _machine(client, "rt-m2-out@example.com", opted_in=False, hostname="not-opted")
    asleep = await _machine(client, "rt-m2-off@example.com", hostname="maintenance")
    async with get_sessionmaker()() as db:
        (await db.get(ProviderMachine, uuid.UUID(asleep["machine_id"]))).availability = "maintenance"
        await db.commit()
    banned = await _machine(client, "rt-m2-ban@example.com", hostname="banned")
    async with get_sessionmaker()() as db:
        (await db.get(ProviderMachine, uuid.UUID(banned["machine_id"]))).is_banned = True
        await db.commit()
    windows = await _machine(client, "rt-m2-win@example.com", hostname="win")
    await _set_flags(windows["machine_id"], True, os_name="windows")  # Windows never keeps the flag
    for target in (out["machine_id"], asleep["machine_id"], banned["machine_id"], windows["machine_id"], str(uuid.uuid4())):
        r = await _create(client, h, machine_id=target)
        assert r.status_code == 422 and r.json()["detail"] == "machine_not_available_for_rental", (target, r.text)


# ---- several workers with the in-memory relay: refuse loudly -------------------------------------------------------------
@pytest.mark.parametrize("workers,backend,refused", [
    ("2", "memory", True), ("4", "", True), ("2", "redis", False), ("2", "REDIS", False), ("1", "memory", False), (None, "memory", False),
    ("", "memory", False), ("abc", "memory", False), ("0", "memory", False),
])
async def test_a_rental_is_refused_when_workers_and_relay_do_not_fit(client, rental_on, monkeypatch, workers, backend, refused):
    if workers is None:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", workers)
    monkeypatch.setattr(settings, "SESSIONS_RELAY_BACKEND", backend)
    h = await _login(client, f"rt-relay-{uuid.uuid4().hex[:8]}@example.com")
    r = await _create(client, h)
    if refused:
        assert r.status_code == 503 and r.json()["detail"] == "session_relay_not_ready"
        sessions = (await client.get("/v1/sessions", headers=h)).json()
        assert (sessions["items"] if isinstance(sessions, dict) else sessions) == []  # nothing held, nothing created
    else:
        assert r.status_code == 201, r.text


async def test_a_kit_session_is_not_touched_by_the_relay_rule(client, rental_on, monkeypatch):
    from tests.test_sessions_routes import _kit

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setattr(settings, "SESSIONS_RELAY_BACKEND", "memory")
    h = await _login(client, "rt-relay-kit@example.com")
    r = await client.post("/v1/sessions", headers=h, json={"kit_ref": await _kit(), "max_price_per_gpu_hour": "2.00", "client_pubkey": PUBKEY})
    assert r.status_code == 201


# ---- create: input files --------------------------------------------------------------------------------------------------
async def test_input_files_are_checked_and_recorded_without_urls(client, rental_on, store):
    h = await _login(client, "rt-in@example.com")
    f1, d1 = await _ready_file(client, store, "rt-in@example.com", name="faces.zip")
    f2, _ = await _ready_file(client, store, "rt-in@example.com", name="labels.csv", data=b"y" * 2048)
    r = await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": "data/faces.zip"}, {"file_id": f2, "path": "labels.csv"}])
    assert r.status_code == 201, r.text
    assert r.json()["inputs"] == [{"path": "data/faces.zip", "size_bytes": len(d1)}, {"path": "labels.csv", "size_bytes": 2048}]
    row = await _row(r.json()["id"])
    assert row.inputs[0]["sha256"] == sha(d1) and row.inputs[0]["file_id"] == f1 and "url" not in row.inputs[0]


async def test_input_files_need_a_workspace_to_land_in(client, rental_on, store):
    h = await _login(client, "rt-in2@example.com")
    f1, _ = await _ready_file(client, store, "rt-in2@example.com")
    r = await _create(client, h, workspace_gb=0, inputs=[{"file_id": f1, "path": "a.bin"}])
    assert r.status_code == 422 and r.json()["detail"] == "workspace_required_for_inputs"


async def test_someone_elses_or_missing_files_look_the_same(client, rental_on, store):
    await register_verify_login(client, "rt-in3-owner@example.com")
    theirs, _ = await _ready_file(client, store, "rt-in3-owner@example.com")
    h = await _login(client, "rt-in3@example.com")
    for fid in (theirs, str(uuid.uuid4())):
        r = await _create(client, h, workspace_gb=5, inputs=[{"file_id": fid, "path": "a.bin"}])
        assert r.status_code == 404 and r.json()["detail"] == "input_file_not_found"


@pytest.mark.parametrize("path", ["../x", "/abs", "a/../b", "a//b", "./a", "a/./b", "results/a", "results", "a\\b", "", "a b ", " a", "x" * 241,
                                  "a" * 101, "a;b", "a\x00b", "a\nb", "$(id)"])
async def test_input_paths_must_be_clean_relative_paths(client, rental_on, store, path):
    h = await _login(client, f"rt-path-{uuid.uuid4().hex[:8]}@example.com")
    email = (await client.get("/auth/me", headers=h)).json()["email"]
    f1, _ = await _ready_file(client, store, email)
    r = await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": path}])
    assert r.status_code in (404, 422) and r.status_code != 201, (path, r.text)


async def test_good_input_paths_are_accepted(client, rental_on, store):
    h = await _login(client, "rt-goodpath@example.com")
    f1, _ = await _ready_file(client, store, "rt-goodpath@example.com")
    for path in ("a.bin", "data/set 1/a_b-c.txt", "results2/x"):
        r = await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": path}])
        assert r.status_code == 201, (path, r.text)
        await client.post(f"/v1/sessions/{r.json()['id']}/stop", headers=h)


async def test_the_same_path_twice_a_directory_and_a_file_and_too_many_inputs_are_refused(client, rental_on, store, monkeypatch):
    h = await _login(client, "rt-conflict@example.com")
    f1, _ = await _ready_file(client, store, "rt-conflict@example.com", name="a")
    f2, _ = await _ready_file(client, store, "rt-conflict@example.com", name="b", data=b"z" * 99)
    for inputs in ([{"file_id": f1, "path": "a"}, {"file_id": f2, "path": "a"}],
                   [{"file_id": f1, "path": "d"}, {"file_id": f2, "path": "d/x"}],
                   [{"file_id": f1, "path": "a"}, {"file_id": f1, "path": "b"}]):
        r = await _create(client, h, workspace_gb=5, inputs=inputs)
        assert r.status_code == 422 and r.json()["detail"] == "input_paths_conflict", (inputs, r.text)
    monkeypatch.setattr(settings, "RENTAL_MAX_INPUTS", 1)
    r = await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": "a"}, {"file_id": f2, "path": "b"}])
    assert r.status_code == 422 and r.json()["detail"] == "too_many_inputs"


async def test_a_file_that_is_not_ready_cannot_be_an_input(client, rental_on, store):
    from app.models.data_plane import DataObject

    h = await _login(client, "rt-notready@example.com")
    f1, _ = await _ready_file(client, store, "rt-notready@example.com")
    async with get_sessionmaker()() as db:
        from app.models.data_plane import DataFile

        obj = await db.get(DataObject, (await db.get(DataFile, uuid.UUID(f1))).object_id)
        obj.status = "pending"
        await db.commit()
    r = await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": "a"}])
    assert r.status_code == 404 and r.json()["detail"] == "input_file_not_found"


# ---- money is the existing session settlement -----------------------------------------------------------------------------
async def _ledger_kinds(sid) -> dict[str, int]:
    """The session's postings by idempotency key prefix: hold, capture, release (the existing settlement; rental adds none)."""
    from app.models.billing import LedgerTransaction

    async with get_sessionmaker()() as db:
        rows = (await db.execute(select(LedgerTransaction).where(LedgerTransaction.reference_id == str(sid)))).scalars().all()
        return {t.idempotency_key.split(":")[0]: t.id for t in rows}


async def test_a_rental_that_never_started_costs_nothing_and_one_that_ran_is_billed_by_the_existing_settlement(client, rental_on):
    m = await _machine(client, "rt-bill-host@example.com")
    h = await _login(client, "rt-bill@example.com")
    sid = (await _create(client, h)).json()["id"]
    unused = await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    assert unused.status_code == 200 and unused.json()["status"] == SESSION_STOPPED
    assert float(unused.json()["captured"] or 0) == 0 and unused.json()["active_seconds"] in (0, None)
    assert set(await _ledger_kinds(sid)) == {"sess-hold", "sess-release"}  # held, then the whole hold released: nothing captured
    sid2 = (await _create(client, h)).json()["id"]
    await _make_active(client, m, sid2)
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(ComputeSession).where(ComputeSession.id == uuid.UUID(sid2)))).scalar_one()
        row.started_at = utcnow() - dt.timedelta(seconds=600)
        await db.commit()
    stopped = await client.post(f"/v1/sessions/{sid2}/stop", headers=h)
    assert stopped.status_code == 200 and stopped.json()["status"] == SESSION_STOPPED and float(stopped.json()["captured"]) > 0
    assert stopped.json()["active_seconds"] >= 600
    assert set(await _ledger_kinds(sid2)) == {"sess-hold", "sess-capture", "sess-release"}
    again = await client.post(f"/v1/sessions/{sid2}/stop", headers=h)  # a second stop never bills twice
    assert again.status_code == 200 and again.json()["captured"] == stopped.json()["captured"]


# ---- claim ----------------------------------------------------------------------------------------------------------------
async def test_only_a_host_that_opted_in_gets_a_rental(client, rental_on):
    out = await _machine(client, "rt-c1-out@example.com", opted_in=False, hostname="c-out")
    inn = await _machine(client, "rt-c1-in@example.com", hostname="c-in")
    h = await _login(client, "rt-c1@example.com")
    sid = (await _create(client, h)).json()["id"]
    assert (await _claim_session(client, out)).json() == {"session": None}  # not opted in
    got = (await _claim_session(client, inn)).json()["session"]
    assert got["id"] == sid and got["rental"] is True and got["template"] == "pytorch-jupyter"
    assert got["workspace_gb"] == 0 and got["memory_mb"] == 16384 and got["cpus"] == 4 and got["inputs"] == []
    assert got["export_window_seconds"] == 1800 and got["command"] is None and got["mode"] == "tcp" and got["tcp_port"] == 8888
    assert got["image_digest"] == TEMPLATE_IMAGE and got["isolation_tier"] == "container"


async def test_a_kit_session_still_goes_to_a_host_that_did_not_opt_in(client, rental_on):
    from tests.test_sessions_routes import _kit

    out = await _machine(client, "rt-c2-out@example.com", opted_in=False, hostname="c2-out")
    h = await _login(client, "rt-c2@example.com")
    sid = (await client.post("/v1/sessions", headers=h, json={"kit_ref": await _kit(), "max_price_per_gpu_hour": "2.00", "client_pubkey": PUBKEY})).json()["id"]
    got = (await _claim_session(client, out)).json()["session"]
    assert got["id"] == sid and got["rental"] is False


async def test_a_session_aimed_at_one_machine_is_only_that_machines(client, rental_on):
    a = await _machine(client, "rt-c3-a@example.com", hostname="c3-a")
    b = await _machine(client, "rt-c3-b@example.com", hostname="c3-b")
    h = await _login(client, "rt-c3@example.com")
    sid = (await _create(client, h, machine_id=a["machine_id"])).json()["id"]
    assert (await _claim_session(client, b)).json() == {"session": None}
    assert (await _claim_session(client, a)).json()["session"]["id"] == sid


async def test_the_claim_lists_inputs_without_urls(client, rental_on, store):
    m = await _machine(client, "rt-c4-host@example.com")
    h = await _login(client, "rt-c4@example.com")
    f1, d1 = await _ready_file(client, store, "rt-c4@example.com", name="faces.zip")
    sid = (await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": "faces.zip"}])).json()["id"]
    got = (await _claim_session(client, m)).json()["session"]
    assert got["id"] == sid and got["inputs"] == [{"path": "faces.zip", "size_bytes": len(d1), "sha256": sha(d1)}]
    assert "url" not in json.dumps(got) and got["workspace_gb"] == 5


async def test_a_price_above_the_customers_maximum_is_not_claimed(client, rental_on):
    m = await create_listable_machine(client, "rt-c5-host@example.com", hostname="pricey", price="9.00")
    await _set_flags(m["machine_id"], True)
    h = await _login(client, "rt-c5@example.com")
    await _create(client, h, max_price_per_gpu_hour="1.00")
    assert (await _claim_session(client, m)).json() == {"session": None}


# ---- a host with no network-filter proxy cannot start a rental that asks for internet sites -------------------------------
async def test_a_host_without_a_network_filter_is_not_handed_a_rental_that_needs_the_internet(client, rental_on):
    plain = await _machine(client, "rt-np-host@example.com", hostname="np")
    await _set_flags(plain["machine_id"], True, proxy=False)
    h = await _login(client, "rt-np@example.com")
    needs = (await _create(client, h)).json()["id"]  # the template allows package sites
    offline = (await _create(client, h, template=None, image_digest=GHCR, mode="pty")).json()["id"]  # a custom image: no network at all
    got = (await _claim_session(client, plain)).json()["session"]
    assert got["id"] == offline  # it takes the one that needs no network...
    assert (await _claim_session(client, plain)).json() == {"session": None}  # ...and never the other
    proxied = await _machine(client, "rt-np2-host@example.com", hostname="np2")
    assert (await _claim_session(client, proxied)).json()["session"]["id"] == needs  # a host with the filter takes it


async def test_picking_a_host_without_a_network_filter_for_a_rental_that_needs_the_internet_is_refused(client, rental_on):
    plain = await _machine(client, "rt-np3-host@example.com", hostname="np3")
    await _set_flags(plain["machine_id"], True, proxy=False)
    h = await _login(client, "rt-np3@example.com")
    r = await _create(client, h, machine_id=plain["machine_id"])
    assert r.status_code == 422 and r.json()["detail"] == "machine_cannot_filter_network"
    ok = await _create(client, h, template=None, image_digest=GHCR, mode="pty", machine_id=plain["machine_id"])  # no network asked: fine
    assert ok.status_code == 201


# ---- one GPU user at a time -----------------------------------------------------------------------------------------------
async def test_a_machine_running_a_job_is_not_handed_a_session(client, rental_on):
    await seed_echo_model()
    m = await _machine(client, "rt-g1-host@example.com", hostname="g1")
    cust = await register_verify_login(client, "rt-g1-job@example.com")
    job = (await submit_job(client, cust["access_token"], machine_id=m["machine_id"])).json()
    assert (await claim_job(client, m["machine_id"], m["secret"])).status_code == 200
    from app.models.jobs import Job
    from app.services import job_fsm as fsm

    async with get_sessionmaker()() as db:
        j = (await db.execute(select(Job).where(Job.id == uuid.UUID(job["job_id"])))).scalar_one()
        j.status = fsm.RUNNING
        await db.commit()
    h = await _login(client, "rt-g1@example.com")
    await _create(client, h)
    assert (await _claim_session(client, m)).json() == {"session": None}  # the GPU is busy with the job


async def test_a_machine_with_a_live_gpu_session_is_not_handed_a_job(client, rental_on):
    await seed_echo_model()
    m = await _machine(client, "rt-g2-host@example.com", hostname="g2")
    cust = await register_verify_login(client, "rt-g2-job@example.com")
    await submit_job(client, cust["access_token"], machine_id=m["machine_id"])
    h = await _login(client, "rt-g2@example.com")
    sid = (await _create(client, h)).json()["id"]
    await _make_active(client, m, sid)
    assert (await claim_job(client, m["machine_id"], m["secret"])).status_code == 204  # the GPU is busy with the session
    await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    assert (await claim_job(client, m["machine_id"], m["secret"])).status_code == 200  # free again: the job goes through


async def test_a_session_without_a_gpu_does_not_block_jobs(client, rental_on):
    await seed_echo_model()
    m = await _machine(client, "rt-g3-host@example.com", hostname="g3")
    cust = await register_verify_login(client, "rt-g3-job@example.com")
    await submit_job(client, cust["access_token"], machine_id=m["machine_id"])
    h = await _login(client, "rt-g3@example.com")
    sid = (await _create(client, h, gpu_count=0)).json()["id"]
    await _make_active(client, m, sid)
    assert (await claim_job(client, m["machine_id"], m["secret"])).status_code == 200


async def test_a_rental_with_no_graphics_card_is_held_like_one_card(client, rental_on):
    """CPU-only (gpu_count 0) is accepted and priced as one card, so the host is paid the machine's hourly price."""
    await _machine(client, "rt-cpu-host@example.com", hostname="cpu1")
    h = await _login(client, "rt-cpu@example.com")
    zero = await _create(client, h, gpu_count=0)
    one = await _create(client, h, gpu_count=1)
    two = await _create(client, h, gpu_count=2)
    assert zero.status_code == 201 and one.status_code == 201 and two.status_code == 201
    assert zero.json()["held"] == one.json()["held"] and float(zero.json()["held"]) > 0
    assert float(two.json()["held"]) > float(one.json()["held"])
    assert (await _create(client, h, gpu_count=-1)).json()["detail"] == "gpu_count_out_of_range"
    assert (await _create(client, h, gpu_count=9)).json()["detail"] == "gpu_count_out_of_range"


# ---- the customer's export request ----------------------------------------------------------------------------------------
async def test_export_needs_an_active_rental_owned_by_the_caller(client, rental_on):
    from tests.test_sessions_routes import _kit

    m = await _machine(client, "rt-e1-host@example.com")
    h = await _login(client, "rt-e1@example.com")
    other = await _login(client, "rt-e1-other@example.com")
    sid = (await _create(client, h)).json()["id"]
    assert (await client.post(f"/v1/sessions/{sid}/export", headers=h)).json()["detail"] == "session_not_exportable"  # still pending
    await _make_active(client, m, sid)
    assert (await client.post(f"/v1/sessions/{sid}/export", headers=other)).status_code == 404  # someone else's
    assert (await client.post(f"/v1/sessions/{uuid.uuid4()}/export", headers=h)).status_code == 404
    ok = await client.post(f"/v1/sessions/{sid}/export", headers=h)
    assert ok.status_code == 200 and ok.json() == {"export_state": "requested"}
    again = await client.post(f"/v1/sessions/{sid}/export", headers=h)
    assert again.status_code == 200 and again.json() == {"export_state": "requested"}  # unchanged, not stacked
    kit = (await client.post("/v1/sessions", headers=h, json={"kit_ref": await _kit(), "max_price_per_gpu_hour": "2.00", "client_pubkey": PUBKEY})).json()["id"]
    assert (await client.post(f"/v1/sessions/{kit}/export", headers=h)).json()["detail"] == "session_not_exportable"  # a Kit session
    assert (await client.post(f"/v1/sessions/{sid}/export")).status_code in (401, 403)


async def test_export_is_rate_limited_once_it_has_finished(client, rental_on):
    m = await _machine(client, "rt-e2-host@example.com")
    h = await _login(client, "rt-e2@example.com")
    sid = (await _create(client, h)).json()["id"]
    await _make_active(client, m, sid)
    assert (await client.post(f"/v1/sessions/{sid}/export", headers=h)).status_code == 200
    res = await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-result", {"files": [], "failed": False})
    assert res.status_code == 200 and (await _row(sid)).export_state == "done"
    soon = await client.post(f"/v1/sessions/{sid}/export", headers=h)
    assert soon.status_code == 429 and soon.json()["detail"] == "export_too_soon"
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(ComputeSession).where(ComputeSession.id == uuid.UUID(sid)))).scalar_one()
        row.export_requested_at = utcnow() - dt.timedelta(seconds=120)
        await db.commit()
    assert (await client.post(f"/v1/sessions/{sid}/export", headers=h)).status_code == 200


async def test_the_heartbeat_tells_the_host_to_export_exactly_while_it_is_requested(client, rental_on):
    m = await _machine(client, "rt-e3-host@example.com")
    h = await _login(client, "rt-e3@example.com")
    sid = (await _create(client, h)).json()["id"]
    await _make_active(client, m, sid)
    hb = await _host_post(client, m, f"/v1/agent/sessions/{sid}/heartbeat")
    assert hb.json() == {"stop": False, "reason": None, "export": False}
    await client.post(f"/v1/sessions/{sid}/export", headers=h)
    assert (await _host_post(client, m, f"/v1/agent/sessions/{sid}/heartbeat")).json()["export"] is True
    assert (await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-start")).json() == {"state": "running"}
    assert (await _host_post(client, m, f"/v1/agent/sessions/{sid}/heartbeat")).json()["export"] is False  # running: not asked twice


# ---- the host's file routes -----------------------------------------------------------------------------------------------
async def test_the_host_gets_fresh_links_only_for_the_listed_files(client, rental_on, store):
    m = await _machine(client, "rt-f1-host@example.com")
    h = await _login(client, "rt-f1@example.com")
    f1, d1 = await _ready_file(client, store, "rt-f1@example.com", name="faces.zip")
    await _ready_file(client, store, "rt-f1@example.com", name="secret.txt", data=b"not listed")
    sid = (await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": "faces.zip"}])).json()["id"]
    await _make_active(client, m, sid)
    r = await _host_post(client, m, f"/v1/agent/sessions/{sid}/inputs")
    assert r.status_code == 200, r.text
    items = r.json()["inputs"]
    assert len(items) == 1 and items[0]["path"] == "faces.zip" and items[0]["sha256"] == sha(d1) and items[0]["size_bytes"] == len(d1)
    assert items[0]["url"].startswith("fake-get://") and items[0]["expires_at"]
    assert (await _host_post(client, m, f"/v1/agent/sessions/{sid}/inputs")).status_code == 200  # refreshed on demand


async def test_the_input_route_refuses_the_wrong_machine_a_kit_session_and_a_finished_one(client, rental_on, store):
    from tests.test_sessions_routes import _kit

    m = await _machine(client, "rt-f2-host@example.com", hostname="f2")
    other = await _machine(client, "rt-f2-other@example.com", hostname="f2b")
    h = await _login(client, "rt-f2@example.com")
    sid = (await _create(client, h)).json()["id"]
    await _make_active(client, m, sid)
    assert (await _host_post(client, other, f"/v1/agent/sessions/{sid}/inputs")).status_code == 404  # not that machine's session
    await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    assert (await _host_post(client, m, f"/v1/agent/sessions/{sid}/inputs")).status_code == 409  # stopped: no more downloads
    # a Kit session claimed by the machine is not a rental
    kit = (await client.post("/v1/sessions", headers=h, json={"kit_ref": await _kit(), "max_price_per_gpu_hour": "2.00", "client_pubkey": PUBKEY})).json()["id"]
    await _make_active(client, m, kit)
    assert (await _host_post(client, m, f"/v1/agent/sessions/{kit}/inputs")).json()["detail"] == "session_not_rental"


async def test_an_input_that_changed_or_vanished_stops_the_download(client, rental_on, store):
    from app.models.data_plane import DataFile

    m = await _machine(client, "rt-f3-host@example.com")
    h = await _login(client, "rt-f3@example.com")
    f1, _ = await _ready_file(client, store, "rt-f3@example.com")
    sid = (await _create(client, h, workspace_gb=5, inputs=[{"file_id": f1, "path": "a"}])).json()["id"]
    await _make_active(client, m, sid)
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(ComputeSession).where(ComputeSession.id == uuid.UUID(sid)))).scalar_one()
        row.inputs = [{**row.inputs[0], "sha256": "0" * 64}]
        await db.commit()
    r = await _host_post(client, m, f"/v1/agent/sessions/{sid}/inputs")
    assert r.status_code == 409 and r.json()["detail"] == "input_changed"


async def _http_upload(client, machine, sid, name, data, *, store_, content_type=None):
    """The agent's multipart upload through the session routes, with the fake store receiving the parts."""
    start = await _host_post(client, machine, f"/v1/agent/sessions/{sid}/uploads",
                             {"name": name, "sha256": sha(data), "size_bytes": len(data), **({"content_type": content_type} if content_type else {})})
    if start.status_code != 200:
        return start, None
    st = start.json()
    if st["status"] != "complete":
        for n in range(1, st["part_count"] + 1):
            chunk = data[(n - 1) * st["part_size"]: n * st["part_size"]]
            url = await _host_post(client, machine, f"/v1/agent/sessions/{sid}/uploads/{st['upload_id']}/parts/{n}/url",
                                   {"sha256": sha(chunk), "size_bytes": len(chunk)})
            assert url.status_code == 200, url.text
            up = await _upload_row(st["upload_id"])
            store_.client_put(up.store_upload_id, n, chunk, sha(chunk))
            conf = await _host_post(client, machine, f"/v1/agent/sessions/{sid}/uploads/{st['upload_id']}/parts/{n}/confirm")
            assert conf.status_code == 200, conf.text
    done = await _host_post(client, machine, f"/v1/agent/sessions/{sid}/uploads/{st['upload_id']}/complete")
    return done, st


async def _upload_row(upload_id):
    from app.models.data_plane import DataUpload

    async with get_sessionmaker()() as db:
        return (await db.execute(select(DataUpload).where(DataUpload.id == uuid.UUID(upload_id)))).scalar_one()


async def test_the_host_can_save_a_result_file_into_the_customers_storage(client, rental_on, store):
    from app.models.data_plane import DataFile

    m = await _machine(client, "rt-u1-host@example.com")
    h = await _login(client, "rt-u1@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    data = b"model-weights" * 1000
    done, _ = await _http_upload(client, m, sid, "results/model.pt", data, store_=store)
    assert done.status_code == 200 and done.json()["file_id"], done.text
    file_id = done.json()["file_id"]
    async with get_sessionmaker()() as db:
        f = await db.get(DataFile, uuid.UUID(file_id))
        assert f.owner_id == await get_user_id("rt-u1@example.com") and f.name == f"session/{sid}/results/model.pt"
    res = await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-result",
                           {"files": [{"name": "results/model.pt", "size_bytes": len(data), "sha256": sha(data), "file_id": file_id}], "failed": False})
    assert res.status_code == 200 and res.json() == {"export_state": "done", "files": 1}
    shown = (await client.get(f"/v1/sessions/{sid}", headers=h)).json()
    assert shown["exports"] == [{"name": "results/model.pt", "size_bytes": len(data), "file_id": file_id}] and shown["export_state"] == "done"


async def test_the_same_upload_twice_is_one_file(client, rental_on, store):
    m = await _machine(client, "rt-u2-host@example.com")
    h = await _login(client, "rt-u2@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    data = b"same" * 500
    first, _ = await _http_upload(client, m, sid, "results/a.bin", data, store_=store)
    second, _ = await _http_upload(client, m, sid, "results/a.bin", data, store_=store)
    assert first.json()["file_id"] == second.json()["file_id"]


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a/../b", "a//b", "a\\b", "", "x" * 241, "a\x00b", "a;b"])
async def test_upload_names_must_be_clean_relative_paths(client, rental_on, store, name):
    m = await _machine(client, f"rt-u3-host-{uuid.uuid4().hex[:6]}@example.com", hostname=f"u3{uuid.uuid4().hex[:4]}")
    h = await _login(client, f"rt-u3-{uuid.uuid4().hex[:6]}@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    r = await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", {"name": name, "sha256": sha(b"x"), "size_bytes": 1})
    assert r.status_code == 422, (name, r.text)


@pytest.mark.parametrize("body", [{}, {"name": "a", "sha256": "xyz", "size_bytes": 5}, {"name": "a", "sha256": "0" * 64, "size_bytes": 0},
                                  {"name": "a", "sha256": "0" * 64, "size_bytes": -1}, {"name": "a", "sha256": "0" * 64, "size_bytes": True},
                                  {"name": "a", "sha256": "0" * 64, "size_bytes": "5"}, {"name": "a", "sha256": "0" * 64, "size_bytes": 1 << 51}])
async def test_upload_hash_and_size_are_validated(client, rental_on, store, body):
    m = await _machine(client, f"rt-u4-host-{uuid.uuid4().hex[:6]}@example.com", hostname=f"u4{uuid.uuid4().hex[:4]}")
    h = await _login(client, f"rt-u4-{uuid.uuid4().hex[:6]}@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    assert (await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", body)).status_code == 422


async def test_only_the_sessions_own_machine_can_upload_and_not_for_a_kit_session(client, rental_on, store):
    from tests.test_sessions_routes import _kit

    m = await _machine(client, "rt-u5-host@example.com", hostname="u5")
    other = await _machine(client, "rt-u5-other@example.com", hostname="u5b")
    h = await _login(client, "rt-u5@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    body = {"name": "results/a", "sha256": sha(b"x"), "size_bytes": 1}
    assert (await _host_post(client, other, f"/v1/agent/sessions/{sid}/uploads", body)).status_code == 404
    kit = (await client.post("/v1/sessions", headers=h, json={"kit_ref": await _kit(), "max_price_per_gpu_hour": "2.00", "client_pubkey": PUBKEY})).json()["id"]
    await _make_active(client, m, kit)
    assert (await _host_post(client, m, f"/v1/agent/sessions/{kit}/uploads", body)).json()["detail"] == "session_not_rental"


async def test_uploads_are_accepted_after_the_rental_ends_only_inside_the_export_window(client, rental_on, store):
    m = await _machine(client, "rt-w1-host@example.com")
    h = await _login(client, "rt-w1@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    inside, _ = await _http_upload(client, m, sid, "results/late.bin", b"late" * 100, store_=store)
    assert inside.status_code == 200, inside.text  # the host exports AFTER the API ended the session
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(ComputeSession).where(ComputeSession.id == uuid.UUID(sid)))).scalar_one()
        row.stopped_at = utcnow() - dt.timedelta(seconds=1801)
        await db.commit()
    late = await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", {"name": "results/x", "sha256": sha(b"x"), "size_bytes": 1})
    assert late.status_code == 409 and late.json()["detail"] == "export_window_closed"
    res = await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-result", {"files": [], "failed": False})
    assert res.status_code == 409


async def test_an_upload_id_from_another_session_is_not_usable(client, rental_on, store):
    m = await _machine(client, "rt-x1-host@example.com")
    h = await _login(client, "rt-x1@example.com")
    s1 = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, s1)
    start = (await _host_post(client, m, f"/v1/agent/sessions/{s1}/uploads", {"name": "results/a", "sha256": sha(b"x" * 10), "size_bytes": 10})).json()
    await client.post(f"/v1/sessions/{s1}/stop", headers=h)
    s2 = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, s2)
    r = await _host_post(client, m, f"/v1/agent/sessions/{s2}/uploads/{start['upload_id']}/complete")
    assert r.status_code == 404


async def test_the_number_of_exported_files_and_their_total_size_are_capped(client, rental_on, store, monkeypatch):
    m = await _machine(client, "rt-cap-host@example.com")
    h = await _login(client, "rt-cap@example.com")
    sid = (await _create(client, h, workspace_gb=1)).json()["id"]
    await _make_active(client, m, sid)
    monkeypatch.setattr(settings, "RENTAL_MAX_EXPORT_FILES", 2)
    ok1 = await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", {"name": "results/1", "sha256": sha(b"1"), "size_bytes": 1})
    ok2 = await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", {"name": "results/2", "sha256": sha(b"2"), "size_bytes": 1})
    assert ok1.status_code == ok2.status_code == 200
    third = await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", {"name": "results/3", "sha256": sha(b"3"), "size_bytes": 1})
    assert third.status_code == 409 and third.json()["detail"] == "too_many_export_files"
    replay = await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", {"name": "results/1", "sha256": sha(b"1"), "size_bytes": 1})
    assert replay.status_code == 200  # a retry of an upload that exists is not counted twice
    monkeypatch.setattr(settings, "RENTAL_MAX_EXPORT_FILES", 2000)
    big = await _host_post(client, m, f"/v1/agent/sessions/{sid}/uploads", {"name": "results/big", "sha256": sha(b"b"), "size_bytes": 2 * (1 << 30)})
    assert big.status_code == 409 and big.json()["detail"] == "export_too_large"  # more than the 1 GB workspace the customer rented


async def test_the_export_result_must_name_files_this_session_created(client, rental_on, store):
    m = await _machine(client, "rt-r1-host@example.com")
    h = await _login(client, "rt-r1@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    data = b"r" * 300
    done, _ = await _http_upload(client, m, sid, "results/a.bin", data, store_=store)
    fid = done.json()["file_id"]
    good = {"name": "results/a.bin", "size_bytes": len(data), "sha256": sha(data), "file_id": fid}
    stranger_id, _ = await _ready_file(client, store, "rt-r1@example.com", name="mine.bin", data=b"customer owns this but not this session")
    cases = [
        ({**good, "name": "results/other.bin"}, 422),  # the file is stored under another name
        ({**good, "size_bytes": len(data) + 1}, 422),
        ({**good, "sha256": "0" * 64}, 422),
        ({**good, "file_id": stranger_id}, 422),  # a real file of the customer's, but not created by this session
        ({**good, "file_id": str(uuid.uuid4())}, 422),
        ({**good, "file_id": "not-a-uuid"}, 422),
        ({**good, "name": "../x"}, 422),
        ({"name": "results/a.bin"}, 422),
    ]
    for item, status in cases:
        r = await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-result", {"files": [item], "failed": False})
        assert r.status_code == status, (item, r.text)
    for bad in (None, "x", {"a": 1}, [1], [good] * 3000):
        r = await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-result", {"files": bad, "failed": False})
        assert r.status_code == 422
    ok = await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-result", {"files": [good], "failed": True})
    assert ok.status_code == 200 and ok.json()["export_state"] == "failed"  # a failure is recorded, with what was saved
    again = await _host_post(client, m, f"/v1/agent/sessions/{sid}/export-result", {"files": [good], "failed": False})
    assert again.json() == {"export_state": "done", "files": 1}  # the same name replaces, never duplicates


async def test_a_bad_json_body_is_a_422_not_a_500(client, rental_on):
    m = await _machine(client, "rt-j1-host@example.com")
    h = await _login(client, "rt-j1@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    for path in ("uploads", "export-result"):
        for raw in (b"{not json", b"[1]", b'"x"'):
            headers, _ = agent_headers(m["machine_id"], m["secret"], {})
            import hashlib
            import hmac

            ts = str(int(utcnow().timestamp()))
            canonical = f"{m['machine_id']}\n{ts}\n{hashlib.sha256(raw).hexdigest()}"
            sig = hmac.new(m["secret"].removeprefix("msk_").encode(), canonical.encode(), hashlib.sha256).hexdigest()
            headers = {"X-ComputeCoin-Machine": m["machine_id"], "X-ComputeCoin-Timestamp": ts, "X-ComputeCoin-Signature": sig,
                       "Content-Type": "application/json"}
            r = await client.post(f"/v1/agent/sessions/{sid}/{path}", content=raw, headers=headers)
            assert r.status_code == 422, (path, raw, r.status_code, r.text)


async def test_the_host_routes_need_a_valid_machine_signature(client, rental_on):
    m = await _machine(client, "rt-s1-host@example.com")
    h = await _login(client, "rt-s1@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    await _make_active(client, m, sid)
    for path in ("inputs", "export-start", "uploads", "export-result"):
        assert (await client.post(f"/v1/agent/sessions/{sid}/{path}", json={})).status_code in (401, 403, 422), path
        assert (await client.post(f"/v1/agent/sessions/{sid}/{path}", headers=auth_headers("x"), json={})).status_code in (401, 403, 422), path


# ---- templates endpoint ---------------------------------------------------------------------------------------------------
async def test_the_template_list_is_public_and_never_shows_the_image(client, rental_on):
    r = await client.get("/v1/rental/templates")  # no login
    assert r.status_code == 200
    items = r.json()
    assert [t["slug"] for t in items] == ["pytorch-jupyter"]  # the unbuilt and the wrong-registry ones are hidden
    assert set(items[0]) == {"slug", "name", "description", "gpu_required", "mode", "default_egress", "libraries"}
    assert "digest" not in json.dumps(items) and TEMPLATE_IMAGE not in json.dumps(items)


async def test_the_shipped_template_file_offers_nothing_until_an_image_is_built(client, monkeypatch):
    shipped = pathlib.Path(rental_svc.__file__).resolve().parent.parent / "data" / "rental_templates.json"
    monkeypatch.setattr(rental_svc, "TEMPLATE_FILE", shipped)
    monkeypatch.setattr(settings, "CONTAINER_RENTAL_ENABLED", True)
    listed = json.loads(shipped.read_text())["templates"]
    assert {t["slug"] for t in listed} == {"pytorch-jupyter", "vision-jupyter"} and all(t["command"] is None for t in listed)
    built = [t["slug"] for t in listed if t["digest"]]
    assert [t["slug"] for t in (await client.get("/v1/rental/templates")).json()] == built  # hidden while digest is null


@pytest.mark.parametrize("entry", [
    {"slug": "Bad Slug", "mode": "tcp", "tcp_port": 8888},
    {"slug": "x", "mode": "pty", "tcp_port": 8888},
    {"slug": "x", "mode": "tcp", "tcp_port": 0},
    {"slug": "x", "mode": "tcp", "tcp_port": True},
    {"slug": "x", "mode": "tcp", "tcp_port": 8888, "command": []},
    {"slug": "x", "mode": "tcp", "tcp_port": 8888, "command": [""]},
    {"slug": "x", "mode": "tcp", "tcp_port": 8888, "digest": "ghcr.io/a/b:latest"},
    {"slug": "x", "mode": "tcp", "tcp_port": 8888, "default_egress": "pypi.org:443"},
])
async def test_a_malformed_template_entry_is_dropped_never_half_used(rental_on, entry):
    rental_on.write_text(json.dumps({"templates": [entry]}))
    assert rental_svc.available_templates() == [] and rental_svc.get_template("x") is None


async def test_a_missing_or_broken_template_file_means_no_templates(rental_on):
    rental_on.write_text("{not json")
    assert rental_svc.all_templates() == []
    rental_on.unlink()
    assert rental_svc.all_templates() == [] and rental_svc.available_templates() == []


# ---- marketplace ----------------------------------------------------------------------------------------------------------
async def test_the_marketplace_shows_and_filters_rental_hosts(client, rental_on):
    on = await _machine(client, "rt-mk-on@example.com", hostname="mk-on")
    off = await _machine(client, "rt-mk-off@example.com", opted_in=False, hostname="mk-off")
    listed = (await client.get("/v1/marketplace/machines")).json()["items"]
    flags = {i["machine_id"]: i["container_rental"] for i in listed}
    assert flags[on["machine_id"]] is True and flags[off["machine_id"]] is False
    only = (await client.get("/v1/marketplace/machines", params={"container_rental": "true"})).json()["items"]
    assert [i["machine_id"] for i in only] == [on["machine_id"]]
    rest = (await client.get("/v1/marketplace/machines", params={"container_rental": "false"})).json()["items"]
    assert [i["machine_id"] for i in rest] == [off["machine_id"]]
    detail = (await client.get(f"/v1/marketplace/machines/{on['machine_id']}")).json()
    assert detail["container_rental"] is True


async def test_the_marketplace_never_advertises_rental_while_the_platform_switch_is_off(client, rental_on, monkeypatch):
    on = await _machine(client, "rt-mk2-on@example.com", hostname="mk2")
    monkeypatch.setattr(settings, "CONTAINER_RENTAL_ENABLED", False)
    listed = (await client.get("/v1/marketplace/machines")).json()["items"]
    assert [i["container_rental"] for i in listed if i["machine_id"] == on["machine_id"]] == [False]


# ---- the flag ---------------------------------------------------------------------------------------------------------------
def test_the_flag_is_known_and_a_windows_host_never_keeps_it():
    from app.services.capability_flags import KNOWN_FLAGS, WINDOWS_FORBIDDEN_FLAGS, host_capabilities, sanitize_capabilities

    assert "container_rental" in KNOWN_FLAGS and "container_rental" in WINDOWS_FORBIDDEN_FLAGS
    assert sanitize_capabilities(["container_rental", "bogus"]) == ["container_rental"]

    class M:
        last_health = {"platform": {"os": "windows"}, "capabilities": ["container_rental", "container_tier"]}

    assert "container_rental" not in host_capabilities(M()).flags and "container_tier" in host_capabilities(M()).flags


# ---- pure helpers -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("image,expected", [
    ("ghcr.io/a/b@sha256:" + "0" * 64, "ghcr.io"), ("pytorch/pytorch@sha256:" + "0" * 64, "docker.io"),
    ("python@sha256:" + "0" * 64, "docker.io"), ("localhost/x", "localhost"), ("localhost:5000/x", "localhost:5000"),
    ("Registry.Example.com/x", "registry.example.com"), ("", "docker.io"),
])
def test_registry_of(image, expected):
    assert rental_svc.registry_of(image) == expected


@pytest.mark.parametrize("paths,conflict", [(["a", "b"], False), (["a", "a"], True), (["a", "a/b"], True), (["a/b", "a"], True), (["ab", "a"], False),
                                            (["a/b", "a/c"], False), ([], False)])
def test_paths_conflict(paths, conflict):
    assert rental_svc.paths_conflict(paths) is conflict
