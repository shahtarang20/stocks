"""Container rental: the REAL host agent code talks to the REAL API app (no hand-built requests).

Agent side: ``computecoin_agent.session_agent.SessionAgent`` (claim, prepare_rental with input download, export_results through
``runtime_v2.upload_file``, finish_rental). API side: the ASGI app behind the test client. The only fakes are Docker (never started)
and the object store (an in-memory S3 stand-in reached through an HTTP transport, so the agent really PUTs and GETs bytes).
If either side changes a field name, a signature rule or a status code, this fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import types
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.db.session import get_sessionmaker
from app.models.sessions import ComputeSession
from computecoin_agent.executor import DockerExecutor
from computecoin_agent.rental import RentalSettings
from computecoin_agent import session_agent as sa
from sqlalchemy import select
from tests.helpers import auth_headers, get_user_id, register_verify_login
from tests.test_container_rental import (  # noqa: F401  (rental_on is a fixture)
    PUBKEY, TEMPLATE_IMAGE, _body, _create, _login, _machine, _ready_file, rental_on,
)
from tests.test_u2_data_plane import MIB, FakeStore, sha
from app.services.data_plane_store import set_data_store, set_byo_client  # noqa: F401
from app.core.config import settings

pytestmark = pytest.mark.asyncio


class HttpStore(FakeStore):
    """FakeStore whose presigned links are real https URLs that ``store_transport`` serves."""

    def presign_upload_part(self, key, upload_id, part_number, sha256_hex, size_bytes, ttl_seconds):
        return f"https://store.test/put/{upload_id}/{part_number}", {"x-amz-checksum-sha256": sha256_hex}

    def presign_download(self, key, ttl_seconds, filename, content_type):
        return f"https://store.test/get/{key}"

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.strip("/").split("/", 1)
            if request.method == "PUT" and parts[0] == "put":
                uid, n = parts[1].split("/")
                data = request.read()
                try:
                    self.client_put(uid, int(n), data, request.headers.get("x-amz-checksum-sha256", ""))
                except IOError:
                    return httpx.Response(400)
                return httpx.Response(200)
            if request.method == "GET" and parts[0] == "get":
                key = request.url.path[len("/get/"):]
                if key not in self.objects:
                    return httpx.Response(404)
                return httpx.Response(200, content=self.objects[key])
            return httpx.Response(404)

        return httpx.MockTransport(handler)


@pytest.fixture
def http_store(monkeypatch):
    s = HttpStore()
    set_data_store(s)
    for k, v in dict(
        DATA_PLANE_ENABLED=True, DATA_PLANE_ENDPOINT="https://s3.example", DATA_PLANE_BUCKET="b", DATA_PLANE_ACCESS_KEY="k",
        DATA_PLANE_SECRET_KEY_ID="s", DATA_PLANE_PART_SIZE_BYTES=5 * MIB, DATA_PLANE_ACCOUNT_QUOTA_BYTES=10**12,
        DATA_PLANE_MAX_FILE_BYTES=10**12, DATA_PLANE_GC_GRACE_SECONDS=0, DATA_PLANE_RETENTION_DEFAULT_DAYS=0,
        DATA_PLANE_RETENTION_MAX_DAYS=0, DATA_PLANE_CROSS_ACCOUNT_DEDUP=False, DATA_PLANE_INLINE_VERIFY_MAX_BYTES=10**9,
        DATA_PLANE_BYO_ENABLED=True, DATA_PLANE_CREDENTIAL_KEY="k" * 40,
    ).items():
        monkeypatch.setattr(settings, k, v)
    yield s
    set_data_store(None)
    set_byo_client(None)


class Bridge(httpx.BaseTransport):
    """Lets the agent's blocking httpx client call the async ASGI test client from a worker thread."""

    def __init__(self, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        self.client, self.loop = client, loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
        fut = asyncio.run_coroutine_threadsafe(
            self.client.request(request.method, str(request.url), content=body, headers=headers), self.loop
        )
        r = fut.result(120)
        return httpx.Response(r.status_code, headers={"content-type": r.headers.get("content-type", "application/json")}, content=r.content)


class FakeDocker:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _agent(client, loop, machine, tmp_path, store):
    api = sa.SessionApi("http://testserver", machine["machine_id"], machine["secret"], transport=Bridge(client, loop))
    executor = DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True)
    return sa.SessionAgent(
        api, executor, runner=FakeDocker(), workdir=str(tmp_path / "work"),
        rental=RentalSettings(allowed=True, root=str(tmp_path / "rental"), max_workspace_gb=50, max_memory_gb=0, max_cpus=0),
        file_transport=store.transport(),
    )


async def _row(sid):
    async with get_sessionmaker()() as db:
        return (await db.execute(select(ComputeSession).where(ComputeSession.id == uuid.UUID(sid)))).scalar_one()


async def test_a_rental_goes_from_request_to_saved_results_with_the_real_agent(client, rental_on, http_store, tmp_path):
    loop = asyncio.get_running_loop()
    machine = await _machine(client, "ct-host@example.com")
    h = await _login(client, "ct-cust@example.com")
    dataset = os.urandom(6 * MIB + 17)  # more than one part, so the multipart path is real too
    file_id, data = await _ready_file(client, http_store, "ct-cust@example.com", name="faces.zip", data=dataset)
    created = await _create(client, h, workspace_gb=5, memory_mb=8192, cpus=2, inputs=[{"file_id": file_id, "path": "data/faces.zip"}])
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    agent = _agent(client, loop, machine, tmp_path, http_store)

    # 1. claim: the agent reads exactly what the API sends
    claim = await asyncio.to_thread(agent.claim, X25519PrivateKey.generate())
    assert claim and claim["id"] == sid and claim["rental"] is True and claim["inputs"][0]["path"] == "data/faces.zip"

    # 2. workspace and inputs: the real download, size and checksum checked by the agent
    await asyncio.to_thread(agent.prepare_rental, claim)
    ws = agent._runs[sid].workspace
    staged = open(os.path.join(ws.path, "data", "faces.zip"), "rb").read()
    assert staged == dataset
    argv = sa.build_session_argv(agent.executor, claim, agent.workdir, agent._runs[sid])  # the container command from the real claim
    assert "--entrypoint" not in argv and argv[-1] == TEMPLATE_IMAGE  # the template's own ENTRYPOINT runs
    assert argv[argv.index("--workdir") + 1] == "/workspace" and f"type=bind,source={ws.path},target=/workspace" in argv

    # 3. the session starts, runs, and writes results
    status, _ = await asyncio.to_thread(agent.api.post, f"/{sid}/started")
    assert status == 200
    os.makedirs(os.path.join(ws.path, "results", "sub"), exist_ok=True)
    big = os.urandom(5 * MIB + 3)
    with open(os.path.join(ws.path, "results", "model.pt"), "wb") as fh:
        fh.write(big)
    with open(os.path.join(ws.path, "results", "sub", "notes.txt"), "wb") as fh:
        fh.write(b"epoch 3 loss 0.12")
    with open(os.path.join(ws.path, "results", "empty.txt"), "wb"):
        pass  # the store takes no empty files; the agent skips it

    # 4. the customer presses "save results now"; the heartbeat carries it; the agent exports
    assert (await client.post(f"/v1/sessions/{sid}/export", headers=h)).json() == {"export_state": "requested"}
    status, hb = await asyncio.to_thread(agent.api.post, f"/{sid}/heartbeat")
    assert status == 200 and hb["export"] is True
    outcome = await asyncio.to_thread(agent.export_results, claim)
    assert outcome.failed is False and sorted(f["name"] for f in outcome.files) == ["model.pt", "sub/notes.txt"], outcome

    shown = (await client.get(f"/v1/sessions/{sid}", headers=h)).json()
    assert shown["export_state"] == "done" and [e["name"] for e in shown["exports"]] == ["model.pt", "sub/notes.txt"]
    got = {}
    for e in shown["exports"]:  # the customer downloads each saved file from their own storage, bytes intact
        link = await client.post(f"/v1/data/files/{e['file_id']}/download-link", headers=h)
        assert link.status_code == 200, link.text
        key = link.json()["url"].split("https://store.test/get/", 1)[1]
        got[e["name"]] = http_store.objects[key]
    assert got == {"model.pt": big, "sub/notes.txt": b"epoch 3 loss 0.12"}

    # 5. the customer stops; the agent exports once more after the session ended (inside the window), then deletes the workspace
    stopped = await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
    await asyncio.to_thread(agent.finish_rental, claim, True)
    assert not os.path.exists(ws.top) and sid not in agent._runs
    final = (await client.get(f"/v1/sessions/{sid}", headers=h)).json()
    assert final["export_state"] == "done" and [e["name"] for e in final["exports"]] == ["model.pt", "sub/notes.txt"]  # same files, not doubled
    assert len(http_store.objects) == 3  # the dataset + the two results; the second export added nothing new
    row = await _row(sid)
    assert row.terms_accepted_at is not None and row.status == "stopped"


async def test_a_rental_with_a_changed_input_is_refused_by_the_agent_before_anything_starts(client, rental_on, http_store, tmp_path):
    loop = asyncio.get_running_loop()
    machine = await _machine(client, "ct2-host@example.com")
    h = await _login(client, "ct2-cust@example.com")
    file_id, _ = await _ready_file(client, http_store, "ct2-cust@example.com", name="a.bin", data=b"a" * 4096)
    sid = (await _create(client, h, workspace_gb=5, inputs=[{"file_id": file_id, "path": "a.bin"}])).json()["id"]
    agent = _agent(client, loop, machine, tmp_path, http_store)
    claim = await asyncio.to_thread(agent.claim, X25519PrivateKey.generate())
    assert claim["id"] == sid
    for key in list(http_store.objects):  # the stored bytes no longer match the recorded checksum
        http_store.objects[key] = b"b" * 4096
    with pytest.raises(sa.SessionAgentError):
        await agent.serve(claim, X25519PrivateKey.generate())  # fails in prepare_rental, before any container starts
    assert not os.path.exists(os.path.join(str(tmp_path / "rental"), sid)) and sid not in agent._runs  # nothing left on the host
    assert not any(c[:2] == ["docker", "run"] for c in agent._run.calls)  # no container was ever started
    row = await _row(sid)
    assert row.status == "failed" and row.captured_minor in (0, None)  # the API ended it as failed; the customer pays nothing
    kinds = {t.split(":")[0] for t in await _keys(sid)}
    assert kinds == {"sess-hold", "sess-release"}


async def _keys(sid):
    from app.models.billing import LedgerTransaction

    async with get_sessionmaker()() as db:
        rows = (await db.execute(select(LedgerTransaction.idempotency_key).where(LedgerTransaction.reference_id == sid))).scalars().all()
        return list(rows)


async def test_a_host_that_did_not_opt_in_is_never_handed_a_rental_by_the_agent_either(client, rental_on, http_store, tmp_path):
    loop = asyncio.get_running_loop()
    machine = await _machine(client, "ct3-host@example.com", opted_in=False)
    h = await _login(client, "ct3-cust@example.com")
    await _create(client, h)
    agent = _agent(client, loop, machine, tmp_path, http_store)
    assert await asyncio.to_thread(agent.claim, X25519PrivateKey.generate()) is None


async def test_autosave_keeps_the_customers_copy_current_without_sending_unchanged_files(client, rental_on, http_store, tmp_path):
    loop = asyncio.get_running_loop()
    machine = await _machine(client, "ct4-host@example.com")
    h = await _login(client, "ct4-cust@example.com")
    sid = (await _create(client, h, workspace_gb=5)).json()["id"]
    agent = _agent(client, loop, machine, tmp_path, http_store)
    claim = await asyncio.to_thread(agent.claim, X25519PrivateKey.generate())
    await asyncio.to_thread(agent.prepare_rental, claim)
    ws = agent._runs[sid].workspace
    assert (await asyncio.to_thread(agent.api.post, f"/{sid}/started"))[0] == 200
    results = os.path.join(ws.path, "results")
    open(os.path.join(results, "w.pt"), "wb").write(b"v1" * 1000)

    first = await asyncio.to_thread(agent.export_results, claim, autosave=True)  # no button pressed, the customer asked for nothing
    assert [f["name"] for f in first.files] == ["w.pt"] and not first.failed
    shown = (await client.get(f"/v1/sessions/{sid}", headers=h)).json()
    assert shown["export_state"] == "done" and shown["exports"][0]["size_bytes"] == 2000
    objects = len(http_store.objects)

    quiet = await asyncio.to_thread(agent.export_results, claim, autosave=True)
    assert quiet.files == [] and len(http_store.objects) == objects  # unchanged: nothing sent at all

    with open(os.path.join(results, "w.pt"), "wb") as fh:  # training saved a better checkpoint
        fh.write(b"v2-longer" * 1000)
    again = await asyncio.to_thread(agent.export_results, claim, autosave=True)
    assert [f["name"] for f in again.files] == ["w.pt"]
    shown = (await client.get(f"/v1/sessions/{sid}", headers=h)).json()
    assert [(e["name"], e["size_bytes"]) for e in shown["exports"]] == [("w.pt", 9000)]  # the customer's list shows the NEW version
    assert len(http_store.objects) == objects + 1

    # the host's computer dies here: the API ends the session as host-lost; the customer still has the latest checkpoint
    link = await client.post(f"/v1/data/files/{shown['exports'][0]['file_id']}/download-link", headers=h)
    key = link.json()["url"].split("https://store.test/get/", 1)[1]
    assert http_store.objects[key] == b"v2-longer" * 1000
    await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    await asyncio.to_thread(agent.finish_rental, claim, False)
