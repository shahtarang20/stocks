"""Container rental in the session agent (design note section 8): argv, workspace, inputs, quota, export, abuse, heat, owner, opt-in,
and that Kit sessions behave exactly as before. Fake Docker runner, fake API, tmp dirs; nothing real is started."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import stat
import types

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from computecoin_agent import interrupts
from computecoin_agent import rental as rental_mod
from computecoin_agent import session_agent as sa
from computecoin_agent.executor import DockerExecutor
from computecoin_agent.rental import RentalSettings
from test_u10_session_agent import _FakeWs

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="container rental is Linux-only (a Windows host never reports the flag)")

IMG = "img@sha256:" + "a" * 64
SID = "0f0e0d0c-0b0a-4908-8706-050403020100"


@pytest.fixture(autouse=True)
def _clean_registry():
    interrupts.REGISTRY.__init__()
    yield
    interrupts.REGISTRY.__init__()


def _ex():
    return DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True)


def _claim(**kw):
    base = {"id": SID, "image_digest": IMG, "mode": "pty", "gpu_count": 1, "max_duration_seconds": 3600, "host_token": "t1",
            "rental": True, "workspace_gb": 5, "memory_mb": 8192, "cpus": 4, "inputs": [], "network_policy": None}
    base.update(kw)
    return base


def _settings(tmp_path, **kw):
    base = dict(allowed=True, root=str(tmp_path / "rental"), max_workspace_gb=50, max_memory_gb=0, max_cpus=0)
    base.update(kw)
    return RentalSettings(**base)


class Runner:
    """Records every docker command. ``on_run`` is called with the argv of `docker run` (to plant files in the workspace)."""

    def __init__(self, *, top="COMMAND\npython3\n", run_rc=0, on_run=None):
        self.calls, self.top, self.run_rc, self.on_run = [], top, run_rc, on_run

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "run"]:
            if self.on_run:
                self.on_run(argv)
            return types.SimpleNamespace(returncode=self.run_rc, stdout="", stderr="boom" if self.run_rc else "")
        if argv[:2] == ["docker", "top"]:
            return types.SimpleNamespace(returncode=0, stdout=self.top, stderr="")
        if argv[:3] == ["docker", "ps", "-a"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def ran(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def workspace_of(argv):
    mount = next(a for a in argv if a.startswith("type=bind,source=") and a.endswith("target=/workspace"))
    return mount.split("source=", 1)[1].split(",", 1)[0]


class FakeApi:
    def __init__(self, **handlers):
        self.calls, self.handlers = [], handlers

    def relay_url(self, sid, token):
        return "wss://x"

    def post(self, path, body=None):
        self.calls.append((path, body))
        for suffix, fn in self.handlers.items():
            if path.endswith(suffix):
                return fn(body) if callable(fn) else fn
        return 200, {}

    def paths(self):
        return [p for p, _ in self.calls]

    def body(self, suffix):
        return next(b for p, b in self.calls if p.endswith(suffix))


def fake_upload(store):
    def up(adapter, sid, path, *, purpose, name, transport=None, sleeper=None, **_):
        assert purpose == "output" and isinstance(adapter, rental_mod.SessionFileApi)
        data = open(path, "rb").read()
        store.append((name, data))
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "file_id": "fid-" + name.replace("/", "_")}

    return up


def agent_for(tmp_path, api, runner, *, settings=None, **kw):
    kw.setdefault("monitor_seconds", 0.02)
    ag = sa.SessionAgent(api, _ex(), runner=runner, ws_connect=lambda url: _FakeWs(), workdir=str(tmp_path / "work"),
                         rental=settings or _settings(tmp_path), **kw)
    return ag


def run_serve(agent, claim):
    return asyncio.run(asyncio.wait_for(agent.serve(claim, X25519PrivateKey.generate()), 15))


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(sa, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(sa, "HEARTBEAT_RETRY_SECONDS", 0.01)
    real = asyncio.sleep

    async def quick(delay, *a, **k):
        return await real(min(delay, 0.02), *a, **k)

    monkeypatch.setattr(sa.asyncio, "sleep", quick)


# =========================================================================================================== argv
def _run_for(tmp_path, **kw):
    ws = rental_mod.Workspace(str(tmp_path / "rental"), SID, rental_mod.GIB)
    ws.create()
    return rental_mod.RentalRun(workspace=ws, memory_mb=kw.get("memory_mb", 8192), cpus=kw.get("cpus", 4), shm_mb=2048, quota_bytes=rental_mod.GIB)


def test_a_rental_gets_the_workspace_limits_and_environment_and_stays_hardened(tmp_path):
    run = _run_for(tmp_path)
    argv = sa.build_session_argv(_ex(), _claim(), str(tmp_path / "out"), run)
    assert argv[argv.index("--memory") + 1] == "8192m" and argv[argv.index("--cpus") + 1] == "4"
    assert argv[argv.index("--memory-swap") + 1] == "8192m"  # no swap, from the base hardening
    assert argv[argv.index("--shm-size") + 1] == "2048m" and argv[argv.index("--pids-limit") + 1] == str(sa.RENTAL_PIDS_LIMIT)
    assert f"type=bind,source={run.workspace.path},target=/workspace" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    for env in ("HOME=/workspace", "TMPDIR=/workspace/.tmp", "COMPUTECOIN_WORKSPACE=/workspace", "COMPUTECOIN_RESULTS=/workspace/results"):
        assert argv[argv.index(env) - 1] == "-e"
    assert argv[argv.index("--network") + 1] == "none"
    for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--user"):
        assert flag in argv
    assert argv[-1] == IMG and "--privileged" not in argv and "-p" not in argv and "--device" not in argv


def test_a_template_session_runs_the_image_entrypoint_with_the_base_path(tmp_path):
    run = _run_for(tmp_path)
    argv = sa.build_session_argv(_ex(), _claim(mode="tcp", tcp_port=8888, command=None), str(tmp_path / "out"), run)
    assert argv[-1] == IMG and "--entrypoint" not in argv  # the image's own ENTRYPOINT starts Jupyter
    assert any(a.startswith("COMPUTECOIN_SESSION_BASE_PATH=") for a in argv)


def test_a_custom_command_comes_after_the_image_flags(tmp_path):
    run = _run_for(tmp_path)
    argv = sa.build_session_argv(_ex(), _claim(mode="tcp", tcp_port=8888, command=["jupyter", "lab"]), str(tmp_path / "out"), run)
    assert argv[argv.index("--entrypoint") + 1] == "jupyter" and argv[-2:] == [IMG, "lab"]
    assert argv.index("--mount") < argv.index("--entrypoint")


def test_the_gpu_flag_still_follows_the_request(tmp_path):
    run = _run_for(tmp_path)
    assert "--gpus" in sa.build_session_argv(_ex(), _claim(gpu_count=2), str(tmp_path / "o"), run)
    assert "--gpus" not in sa.build_session_argv(_ex(), _claim(gpu_count=0), str(tmp_path / "o"), run)


def test_a_rental_cannot_be_built_without_its_workspace(tmp_path):
    with pytest.raises(sa.SessionAgentError):
        sa.build_session_argv(_ex(), _claim(), str(tmp_path / "o"), None)


def test_private_tiers_and_published_ports_are_still_refused(tmp_path):
    run = _run_for(tmp_path)
    with pytest.raises(sa.SessionAgentError):
        sa.build_session_argv(_ex(), _claim(isolation_tier="sealed"), str(tmp_path / "o"), run)


def test_an_extra_dangerous_flag_from_the_executor_would_be_caught(tmp_path):
    run = _run_for(tmp_path)

    class Weird(DockerExecutor):
        def build_argv_parts(self, spec, out):
            prefix, image, suffix = super().build_argv_parts(spec, out)
            return [*prefix, "--cap-add", "SYS_ADMIN"], image, suffix

    with pytest.raises(sa.SessionAgentError):
        sa.build_session_argv(Weird(docker_available=lambda: True, nvidia_available=lambda: True), _claim(), str(tmp_path / "o"), run)


def test_kit_session_argv_is_unchanged_by_the_rental_feature(tmp_path):
    claim = {"id": "abc", "image_digest": IMG, "gpu_count": 0, "max_duration_seconds": 600}
    argv = sa.build_session_argv(_ex(), claim, "/tmp/w")
    assert "--workdir" not in argv and "--shm-size" not in argv and not any("target=/workspace" in a for a in argv)
    assert not any(a.startswith(("HOME=", "TMPDIR=", "COMPUTECOIN_WORKSPACE")) for a in argv)


# ============================================================================================ opt-in and staging
def test_a_host_that_did_not_opt_in_never_starts_a_rental(tmp_path):
    api = FakeApi()
    runner = Runner()
    ag = agent_for(tmp_path, api, runner, settings=_settings(tmp_path, allowed=False))
    with pytest.raises(sa.SessionAgentError):
        run_serve(ag, _claim())
    assert runner.ran("docker", "run") == []  # nothing was started
    assert "/started" not in " ".join(api.paths()) and api.body("/ended") == {"failed": True}
    assert not os.path.exists(tmp_path / "rental")  # not even a folder


def _inputs_api(items, status=200):
    return FakeApi(**{"/inputs": (status, {"inputs": items})})


def _item(path="data/train.csv", body=b"a,b\n1,2\n", url="https://store.example/x"):
    return {"path": path, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "url": url}


def _want(item):
    return {k: item[k] for k in ("path", "size_bytes", "sha256")}


def test_inputs_are_downloaded_checked_and_made_readable_for_the_container(tmp_path, monkeypatch):
    body = b"a,b\n1,2\n"
    item = _item(body=body)
    api = _inputs_api([item])
    served = []

    def handler(request):
        served.append(str(request.url))
        return httpx.Response(200, content=body)

    ag = agent_for(tmp_path, api, Runner(), file_transport=httpx.MockTransport(handler))
    ag.prepare_rental(_claim(inputs=[_want(item)]))
    dest = os.path.join(ag._runs[SID].workspace.path, "data", "train.csv")
    assert open(dest, "rb").read() == body and served == ["https://store.example/x"]
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o666 and stat.S_IMODE(os.stat(os.path.dirname(dest)).st_mode) == 0o777
    assert api.paths() == [f"/{SID}/inputs"]


def test_a_wrong_checksum_or_size_from_the_store_ends_the_start_and_cleans_up(tmp_path):
    item = _item(body=b"right")
    api = _inputs_api([item])
    ag = agent_for(tmp_path, api, Runner(), file_transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"WRONG")))
    with pytest.raises(sa.SessionAgentError):
        ag.prepare_rental(_claim(inputs=[_want(item)]))
    assert os.listdir(tmp_path / "rental") == [SID]  # registered, so the end-of-session cleanup removes it
    ag.finish_rental(_claim(), False)
    assert os.listdir(tmp_path / "rental") == []


@pytest.mark.parametrize("bad", [
    [{"path": "../x", "size_bytes": 1, "sha256": "a" * 64}], [{"path": "/etc/x", "size_bytes": 1, "sha256": "a" * 64}],
    [{"path": "results/x", "size_bytes": 1, "sha256": "a" * 64}], [{"path": "a b;c", "size_bytes": 1, "sha256": "a" * 64}],
    [{"path": "a", "size_bytes": -1, "sha256": "a" * 64}], [{"path": "a", "size_bytes": True, "sha256": "a" * 64}],
    [{"path": "a", "size_bytes": 1, "sha256": "zz"}], [{"path": "a", "size_bytes": 1}], ["not a dict"],
    [{"path": "a", "size_bytes": 1, "sha256": "a" * 64}, {"path": "a", "size_bytes": 1, "sha256": "a" * 64}],
    [{"path": "a", "size_bytes": 1, "sha256": "a" * 64}, {"path": "a/b", "size_bytes": 1, "sha256": "a" * 64}],
    [{"path": f"f{i}", "size_bytes": 1, "sha256": "a" * 64} for i in range(rental_mod.MAX_INPUTS + 1)],
    [{"path": "huge", "size_bytes": 10 * rental_mod.GIB, "sha256": "a" * 64}],
])
def test_bad_input_lists_are_refused_before_anything_is_created(tmp_path, bad):
    api = FakeApi()
    ag = agent_for(tmp_path, api, Runner())
    with pytest.raises(sa.SessionAgentError):
        ag.prepare_rental(_claim(inputs=bad, workspace_gb=5))
    assert api.calls == [] and not os.path.exists(tmp_path / "rental")


@pytest.mark.parametrize("mutate", [
    lambda it: it.update(size_bytes=it["size_bytes"] + 1),
    lambda it: it.update(sha256="b" * 64),
    lambda it: it.update(url="ftp://store/x"),
    lambda it: it.update(url=None),
    lambda it: it.update(path="other.csv"),
])
def test_a_control_plane_answer_that_differs_from_the_claim_is_refused(tmp_path, mutate):
    item = _item()
    want = _want(item)
    mutate(item)
    ag = agent_for(tmp_path, _inputs_api([item]), Runner(), file_transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")))
    with pytest.raises(sa.SessionAgentError):
        ag.prepare_rental(_claim(inputs=[want]))


def test_an_extra_file_in_the_answer_is_refused_and_a_failed_listing_too(tmp_path):
    a, b = _item("a.csv"), _item("b.csv")
    ag = agent_for(tmp_path, _inputs_api([a, b]), Runner())
    with pytest.raises(sa.SessionAgentError):
        ag.prepare_rental(_claim(inputs=[_want(a)]))
    ag2 = agent_for(tmp_path / "x", _inputs_api([], status=500), Runner())
    with pytest.raises(sa.SessionAgentError):
        ag2.prepare_rental(_claim(inputs=[_want(a)]))


def test_links_are_refreshed_when_staging_takes_long(tmp_path, monkeypatch):
    items = [_item("a.csv", b"1"), _item("b.csv", b"2"), _item("c.csv", b"3")]
    api = _inputs_api(items)
    clock = {"t": 0.0}
    bodies = {i["url"]: b for i, b in zip(items, [b"1", b"2", b"3"])}
    for i in items:
        i["url"] = "https://store.example/" + i["path"]
    bodies = {"https://store.example/a.csv": b"1", "https://store.example/b.csv": b"2", "https://store.example/c.csv": b"3"}

    def handler(request):
        clock["t"] += 200  # each file takes 200 seconds
        return httpx.Response(200, content=bodies[str(request.url)])

    ag = agent_for(tmp_path, api, Runner(), file_transport=httpx.MockTransport(handler), clock=lambda: clock["t"])
    ag.prepare_rental(_claim(inputs=[_want(i) for i in items]))
    assert api.paths().count(f"/{SID}/inputs") == 2  # once at the start, once after the links were 300+ seconds old


def test_not_enough_disk_stops_the_start(tmp_path, monkeypatch):
    monkeypatch.setattr(rental_mod.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10))
    ag = agent_for(tmp_path, FakeApi(), Runner())
    with pytest.raises(sa.SessionAgentError):
        ag.prepare_rental(_claim())
    assert not os.path.exists(tmp_path / "rental" / SID)


def test_a_session_id_that_is_not_an_id_is_refused(tmp_path):
    ag = agent_for(tmp_path, FakeApi(), Runner())
    with pytest.raises(sa.SessionAgentError):
        ag.prepare_rental(_claim(id="../../etc"))


# ========================================================================================================== serve
def _plant(results=None, **files):
    """A runner hook that writes files into the new workspace the moment `docker run` is called."""
    def hook(argv):
        ws = workspace_of(argv)
        for rel, data in (results or {}).items():
            path = os.path.join(ws, "results", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(data)
    return hook


def _beats(stop_after, export_on=None):
    n = {"i": 0}

    def hb(body):
        n["i"] += 1
        return 200, {"stop": n["i"] >= stop_after, "export": export_on is not None and n["i"] == export_on}
    return hb


def test_a_rental_runs_exports_on_request_and_at_the_end_then_deletes_the_workspace(tmp_path):
    store = []
    api = FakeApi(**{"/heartbeat": _beats(6, export_on=2)})
    runner = Runner(on_run=_plant({"model.pt": b"weights", "logs/run.txt": b"log"}))
    ag = agent_for(tmp_path, api, runner, upload=fake_upload(store))
    assert run_serve(ag, _claim()) == "stop"
    paths = api.paths()
    assert paths.index(f"/{SID}/started") < paths.index(f"/{SID}/heartbeat") < paths.index(f"/{SID}/ended")
    # saved twice: when the customer asked, and once more at the end (after the session ended: that time is never billed)
    assert paths.count(f"/{SID}/export-start") == 2 and paths.count(f"/{SID}/export-result") == 2
    last_start = len(paths) - 1 - paths[::-1].index(f"/{SID}/export-start")
    assert last_start > paths.index(f"/{SID}/ended")
    results = [b for p, b in api.calls if p.endswith("/export-result")]
    assert results[0]["failed"] is False and sorted(f["name"] for f in results[0]["files"]) == ["logs/run.txt", "model.pt"]
    assert results[-1]["failed"] is False and results[-1]["files"] == []  # nothing changed since: the control plane already holds both
    assert sorted(n for n, _ in store) == ["logs/run.txt", "model.pt"]  # and each file was sent once, not twice
    assert api.body("/ended") == {"failed": False}
    assert os.listdir(tmp_path / "rental") == []  # the workspace is gone
    assert runner.ran("docker", "rm", "-f", f"ccoin-sess-{SID}")  # and so is the container


def test_the_container_gets_the_hosts_caps_on_memory_and_cpu(tmp_path):
    api = FakeApi(**{"/heartbeat": _beats(3)})
    runner = Runner()
    ag = agent_for(tmp_path, api, runner, settings=_settings(tmp_path, max_memory_gb=4, max_cpus=2), upload=fake_upload([]))
    run_serve(ag, _claim(memory_mb=65536, cpus=32))
    argv = runner.ran("docker", "run")[0]
    assert argv[argv.index("--memory") + 1] == "4096m" and argv[argv.index("--cpus") + 1] == "2"


def test_a_miner_stops_the_rental_at_once_ends_it_as_failed_and_exports_nothing(tmp_path):
    store = []
    api = FakeApi(**{"/heartbeat": (200, {"stop": False})})
    runner = Runner(top="COMMAND\nbash\nxmrig\n", on_run=_plant({"model.pt": b"x"}))
    ag = agent_for(tmp_path, api, runner, upload=fake_upload(store))
    assert run_serve(ag, _claim()) == "stop"
    assert api.body("/ended") == {"failed": True}
    assert "/export-start" not in "".join(api.paths()) and store == []
    assert runner.ran("docker", "rm", "-f", f"ccoin-sess-{SID}")
    assert os.listdir(tmp_path / "rental") == []
    assert "xmrig" not in repr(api.calls)  # the process name never leaves the host


def test_a_full_workspace_stops_the_rental_but_saves_the_results(tmp_path, monkeypatch):
    store = []
    api = FakeApi(**{"/heartbeat": (200, {"stop": False})})
    runner = Runner(on_run=_plant({"keep.bin": b"data"}))
    monkeypatch.setattr(rental_mod, "walk_size", lambda path, **k: (50 * rental_mod.GIB, 10, False))
    ag = agent_for(tmp_path, api, runner, upload=fake_upload(store))
    assert run_serve(ag, _claim(workspace_gb=1)) == "stop"
    assert api.body("/ended") == {"failed": False}  # the customer filled the disk; that is not a failure of the host
    assert store == [("keep.bin", b"data")]
    assert os.listdir(tmp_path / "rental") == []


def test_too_many_files_counts_as_a_full_workspace(tmp_path, monkeypatch):
    api = FakeApi(**{"/heartbeat": (200, {"stop": False})})
    monkeypatch.setattr(rental_mod, "walk_size", lambda path, **k: (10, 9_999_999, True))
    ag = agent_for(tmp_path, api, Runner(), upload=fake_upload([]))
    assert run_serve(ag, _claim()) == "stop"


def test_a_too_hot_card_stops_the_rental_and_saves_the_results(tmp_path):
    from computecoin_agent.thermal import HARD_STOP

    store = []
    api = FakeApi(**{"/heartbeat": (200, {"stop": False})})
    guard = types.SimpleNamespace(check=lambda: HARD_STOP)
    runner = Runner(on_run=_plant({"a.bin": b"1"}))
    ag = agent_for(tmp_path, api, runner, thermal_guard=guard, upload=fake_upload(store))
    assert run_serve(ag, _claim()) == "stop"
    assert api.body("/ended") == {"failed": False} and store == [("a.bin", b"1")]


def test_a_lost_temperature_sensor_also_stops_the_rental(tmp_path):
    from computecoin_agent.thermal import SENSOR_LOST

    api = FakeApi(**{"/heartbeat": (200, {"stop": False})})
    ag = agent_for(tmp_path, api, Runner(), thermal_guard=types.SimpleNamespace(check=lambda: SENSOR_LOST), upload=fake_upload([]))
    assert run_serve(ag, _claim()) == "stop"


def test_a_failing_temperature_check_does_not_end_a_rental(tmp_path):
    def broken():
        raise RuntimeError("driver exploded")

    api = FakeApi(**{"/heartbeat": _beats(5)})
    ag = agent_for(tmp_path, api, Runner(), thermal_guard=types.SimpleNamespace(check=broken), upload=fake_upload([]))
    assert run_serve(ag, _claim()) == "stop"  # ended by the control plane's stop, not by the broken check
    assert len([p for p in api.paths() if p.endswith("/heartbeat")]) >= 5


def test_pause_from_the_owner_stops_a_rental_and_the_registry_is_left_clean(tmp_path):
    api = FakeApi(**{"/heartbeat": (200, {"stop": False})})
    runner = Runner(on_run=lambda argv: interrupts.REGISTRY.interrupt_all("owner_needs_computer"))
    ag = agent_for(tmp_path, api, runner, upload=fake_upload([]))
    assert run_serve(ag, _claim()) == "stop"
    assert api.body("/ended") == {"failed": False}
    assert interrupts.REGISTRY.running() == 0


def test_the_customer_stopping_it_still_saves_and_cleans_up(tmp_path):
    store = []
    api = FakeApi(**{"/heartbeat": (200, {"stop": True, "reason": "user"})})
    ag = agent_for(tmp_path, api, Runner(on_run=_plant({"out.txt": b"done"})), upload=fake_upload(store))
    assert run_serve(ag, _claim()) == "stop"
    assert store == [("out.txt", b"done")] and os.listdir(tmp_path / "rental") == []


def test_input_failure_before_start_ends_failed_and_cleans_up(tmp_path):
    item = _item()
    api = _inputs_api([item], status=500)
    runner = Runner()
    ag = agent_for(tmp_path, api, runner)
    with pytest.raises(sa.SessionAgentError):
        run_serve(ag, _claim(inputs=[_want(item)]))
    assert runner.ran("docker", "run") == [] and "/started" not in "".join(api.paths())
    assert api.body("/ended") == {"failed": True}
    assert os.listdir(tmp_path / "rental") == [] and interrupts.REGISTRY.running() == 0
    assert "/export-start" not in "".join(api.paths())  # nothing ran, so nothing is exported


def test_a_container_that_fails_to_start_still_cleans_everything(tmp_path):
    api = FakeApi()
    ag = agent_for(tmp_path, api, Runner(run_rc=125))
    with pytest.raises(sa.SessionAgentError):
        run_serve(ag, _claim())
    assert api.body("/ended") == {"failed": True} and os.listdir(tmp_path / "rental") == []


def test_an_export_that_fails_never_blocks_the_end(tmp_path):
    def broken_upload(*a, **k):
        raise RuntimeError("store is down")

    api = FakeApi(**{"/heartbeat": _beats(3)})
    ag = agent_for(tmp_path, api, Runner(on_run=_plant({"a": b"1"})), upload=broken_upload)
    assert run_serve(ag, _claim()) == "stop"
    result = [b for p, b in api.calls if p.endswith("/export-result")][-1]
    assert result["failed"] is True and result["files"] == []
    assert "store is down" not in repr(api.calls)  # no raw error text is sent
    assert os.listdir(tmp_path / "rental") == []


def test_a_relay_that_keeps_failing_does_not_loop_forever(tmp_path):
    api = FakeApi(**{"/token": (200, {"host_token": "t2"}), "/heartbeat": (200, {"stop": False})})
    runner = Runner()
    ag = agent_for(tmp_path, api, runner, upload=fake_upload([]))

    class Boom(OSError):
        pass

    def connect(url):
        raise Boom("relay down")

    ag._ws_connect = connect
    claim = _claim(max_duration_seconds=1)
    ag._clock = lambda t=iter(range(0, 10_000)): next(t) * 0.5
    assert run_serve(ag, claim) in ("reconnect", "ended", "stop")
    assert api.body("/ended") == {"failed": False} and os.listdir(tmp_path / "rental") == []


# ------------------------------------------------------------------------------------------------ Kit sessions
def test_a_kit_session_behaves_exactly_as_before(tmp_path):
    api = FakeApi(**{"/heartbeat": _beats(3)})
    runner = Runner()
    ag = agent_for(tmp_path, api, runner, settings=_settings(tmp_path, allowed=False))
    claim = {"id": SID, "image_digest": IMG, "mode": "pty", "gpu_count": 0, "max_duration_seconds": 3600, "host_token": "t1"}
    assert run_serve(ag, claim) == "stop"
    paths = api.paths()
    assert paths[0] == f"/{SID}/started" and paths[-1] == f"/{SID}/ended"
    assert not any(p.endswith(("/inputs", "/export-start", "/export-result", "/uploads")) for p in paths)
    assert api.body("/ended") == {"failed": False}
    assert not os.path.exists(tmp_path / "rental")  # no workspace for a Kit session
    assert not any("--workdir" in c for c in runner.ran("docker", "run"))
    assert runner.ran("docker", "top") == []  # and no rental monitor ran


def test_a_kit_session_ignores_an_export_flag_it_was_never_asked_for(tmp_path):
    api = FakeApi(**{"/heartbeat": _beats(3, export_on=1)})
    ag = agent_for(tmp_path, api, Runner(), settings=_settings(tmp_path, allowed=False))
    run_serve(ag, {"id": SID, "image_digest": IMG, "mode": "pty", "gpu_count": 0, "max_duration_seconds": 3600, "host_token": "t"})
    assert not any(p.endswith("/export-start") for p in api.paths())


# ============================================================================================ upload adapter
def test_the_adapter_maps_job_upload_paths_onto_the_session_routes_and_signs_the_same_way():
    seen = []
    api = types.SimpleNamespace(post=lambda path, body=None: seen.append((path, body)) or (200, {"upload_id": "u1"}))
    ad = rental_mod.SessionFileApi(api, SID)
    assert ad.post(f"/jobs/{SID}/uploads", {"a": 1}) == (200, {"upload_id": "u1"})
    ad.post(f"/jobs/{SID}/uploads/u1/parts/2/url", {"b": 2})
    ad.post(f"/jobs/{SID}/uploads/u1/complete")
    assert [p for p, _ in seen] == [f"/{SID}/uploads", f"/{SID}/uploads/u1/parts/2/url", f"/{SID}/uploads/u1/complete"]
    with pytest.raises(rental_mod.RentalError):
        ad.post(f"/jobs/other/uploads")
    with pytest.raises(rental_mod.RentalError):
        ad.post("/anything-else")


def test_the_adapter_turns_an_error_status_into_the_upload_error():
    from computecoin_agent.runtime_v2 import V2Error

    ad = rental_mod.SessionFileApi(types.SimpleNamespace(post=lambda p, b=None: (409, {"detail": "nope"})), SID)
    with pytest.raises(V2Error) as exc:
        ad.ok(f"/jobs/{SID}/uploads", {}, code="upload_failed")
    assert exc.value.code == "upload_failed"


def test_the_real_upload_routine_runs_through_the_adapter_end_to_end(tmp_path):
    """runtime_v2.upload_file, unchanged, against a fake of the session upload routes and a fake object store."""
    data = b"model weights " * 1000
    sha = hashlib.sha256(data).hexdigest()
    puts, calls = [], []

    class Sessions:
        def post(self, path, body=None):
            calls.append((path, body))
            if path == f"/{SID}/uploads":
                assert body["purpose"] == "output" and body["name"] == "dir/model.bin" and body["sha256"] == sha and body["size_bytes"] == len(data)
                return 200, {"upload_id": "u1", "status": "pending", "part_size": 1 << 20, "part_count": 1, "verified_parts": []}
            if path == f"/{SID}/uploads/u1/parts/1/url":
                return 200, {"url": "https://store.example/part1", "headers": {}}
            if path == f"/{SID}/uploads/u1/parts/1/confirm":
                return 200, {"status": "verified"}
            if path == f"/{SID}/uploads/u1/complete":
                return 200, {"file_id": "file-123"}
            return 404, {}

    transport = httpx.MockTransport(lambda req: puts.append(req.content) or httpx.Response(200))
    ws = rental_mod.Workspace(str(tmp_path / "rental"), SID, rental_mod.GIB)
    ws.create()
    os.makedirs(os.path.join(ws.path, "results", "dir"))
    open(os.path.join(ws.path, "results", "dir", "model.bin"), "wb").write(data)
    run = rental_mod.RentalRun(workspace=ws, memory_mb=1024, cpus=1, shm_mb=64, quota_bytes=rental_mod.GIB)
    out = rental_mod.export_results(Sessions(), SID, run, transport=transport, sleeper=lambda s: None)
    assert out.failed is False and out.files == [{"name": "dir/model.bin", "size_bytes": len(data), "sha256": sha, "file_id": "file-123"}]
    assert puts == [data]  # the bytes went to the store, not through the API
    assert calls[-1][0] == f"/{SID}/export-result" and calls[-1][1]["files"] == out.files
    assert os.listdir(ws.stage) == []  # the staged copy was removed


# ================================================================================================== export rules
def _export_run(tmp_path, files):
    ws = rental_mod.Workspace(str(tmp_path / "rental"), SID, rental_mod.GIB)
    ws.create()
    for rel, data in files.items():
        path = os.path.join(ws.path, "results", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(data)
    return rental_mod.RentalRun(workspace=ws, memory_mb=1024, cpus=1, shm_mb=64, quota_bytes=rental_mod.GIB)


def test_links_and_special_files_in_results_are_never_uploaded_and_cannot_leak_host_files(tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP SECRET")
    run = _export_run(tmp_path, {"ok.txt": b"fine"})
    res = os.path.join(run.workspace.path, "results")
    os.symlink(secret, os.path.join(res, "innocent.txt"))
    os.symlink(tmp_path, os.path.join(res, "folder"))
    os.mkfifo(os.path.join(res, "pipe"))
    store = []
    api = FakeApi()
    out = rental_mod.export_results(api, SID, run, upload=fake_upload(store))
    assert [n for n, _ in store] == ["ok.txt"] and b"TOP SECRET" not in b"".join(d for _, d in store)
    assert [f["name"] for f in out.files] == ["ok.txt"]


def test_a_file_swapped_for_a_link_between_listing_and_copying_is_not_followed(tmp_path, monkeypatch):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP SECRET")
    run = _export_run(tmp_path, {"a.txt": b"fine"})
    res = os.path.join(run.workspace.path, "results")
    real = rental_mod.list_results

    def swap_after_listing(path, **kw):
        out = real(path, **kw)
        os.unlink(os.path.join(res, "a.txt"))
        os.symlink(secret, os.path.join(res, "a.txt"))  # the customer's code wins the race
        return out

    monkeypatch.setattr(rental_mod, "list_results", swap_after_listing)
    store = []
    out = rental_mod.export_results(FakeApi(), SID, run, upload=fake_upload(store))
    assert store == [] and out.failed is True and out.files == []


def test_empty_files_are_skipped_and_the_file_count_is_limited(tmp_path):
    run = _export_run(tmp_path, {"empty": b"", "a": b"1", "b": b"2", "c": b"3"})
    store = []
    out = rental_mod.export_results(FakeApi(), SID, run, upload=fake_upload(store), max_files=2)
    assert out.failed is True and out.error and len(store) <= 2 and "empty" not in [n for n, _ in store]


def test_a_file_over_the_size_limit_is_not_saved_and_is_reported(tmp_path):
    run = _export_run(tmp_path, {"big": b"x" * 100, "small": b"y"})
    store = []
    out = rental_mod.export_results(FakeApi(), SID, run, upload=fake_upload(store), max_file_bytes=50)
    assert [n for n, _ in store] == ["small"] and out.failed is True


def test_an_export_cannot_report_when_the_api_is_down_and_still_does_not_raise(tmp_path):
    run = _export_run(tmp_path, {"a": b"1"})

    class Down:
        def post(self, path, body=None):
            raise ConnectionError("down")

    out = rental_mod.export_results(Down(), SID, run, upload=fake_upload([]))
    assert out.failed is True


def test_two_exports_never_run_at_the_same_time(tmp_path):
    import threading
    import time

    run = _export_run(tmp_path, {"a": b"1"})
    active, peak = [0], [0]

    def slow_upload(adapter, sid, path, **kw):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return {"size": 1, "sha256": "", "file_id": "f"}

    threads = [threading.Thread(target=lambda: rental_mod.export_results(FakeApi(), SID, run, upload=slow_upload)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert peak[0] == 1


# ==================================================================================================== housekeeping
def test_stale_rental_folders_are_removed_but_live_ones_and_unknown_state_are_not(tmp_path):
    root = tmp_path / "rental"
    live, dead = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    for sid in (live, dead):
        (root / sid / "workspace").mkdir(parents=True)

    class R(Runner):
        def __call__(self, argv):
            self.calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=f"ccoin-sess-{live}\nother\n", stderr="")

    ag = agent_for(tmp_path, FakeApi(), R())
    assert ag.cleanup_stale() == 1 and os.listdir(root) == [live]
    # docker cannot be asked: nothing is deleted
    ag2 = agent_for(tmp_path, FakeApi(), lambda argv: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    (root / dead / "workspace").mkdir(parents=True)
    assert ag2.cleanup_stale() == 0 and dead in os.listdir(root)


def test_the_loop_cleans_up_first_and_respects_a_closed_gate(tmp_path):
    claims = []

    class Api(FakeApi):
        def post(self, path, body=None):
            claims.append(path)
            raise KeyboardInterrupt

    api = Api()
    ag = agent_for(tmp_path, api, Runner())
    ag.claim_gate = lambda: False
    sleeps = []
    with pytest.raises(KeyboardInterrupt):
        sa.run_forever(api, _ex(), agent=ag, poll_seconds=0, sleeper=lambda s: (sleeps.append(s), (_ for _ in ()).throw(KeyboardInterrupt))[0])
    assert claims == [] and sleeps == [0]  # a closed gate means no claim at all
    ag.claim_gate = lambda: True
    with pytest.raises(KeyboardInterrupt):
        sa.run_forever(api, _ex(), agent=ag, poll_seconds=0, sleeper=lambda s: None)
    assert claims == ["/claim"]


def test_a_gate_that_raises_counts_as_closed_and_once_returns(tmp_path):
    api = FakeApi()
    ag = agent_for(tmp_path, api, Runner())

    def broken():
        raise RuntimeError("x")

    ag.claim_gate = broken
    sa.run_forever(api, _ex(), agent=ag, once=True, sleeper=lambda s: None)
    assert api.calls == []


# ============================================================================================ background thread
def _cfg(**kw):
    base = dict(serve_sessions=True, api_url="https://api.example", machine_id="m", machine_secret="s", allow_container_rental=False,
                rental_root=None, work_dir=None, rental_max_workspace_gb=50, rental_max_memory_gb=0, rental_max_cpus=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_the_thread_is_not_started_when_sessions_are_off_or_there_is_no_engine_or_on_windows():
    from computecoin_agent.platform import PLATFORM_WINDOWS, use_platform
    from computecoin_agent.platform.fake import make_fake_platform

    assert sa.start_background_sessions(_cfg(serve_sessions=False), engine_available=lambda: True, run_loop=lambda *a, **k: None) is None
    assert sa.start_background_sessions(_cfg(serve_sessions="yes"), engine_available=lambda: True, run_loop=lambda *a, **k: None) is None
    assert sa.start_background_sessions(_cfg(), engine_available=lambda: False, run_loop=lambda *a, **k: None) is None
    assert sa.start_background_sessions(_cfg(), engine_available=lambda: (_ for _ in ()).throw(RuntimeError()), run_loop=lambda *a, **k: None) is None
    with use_platform(make_fake_platform(PLATFORM_WINDOWS)):
        assert sa.start_background_sessions(_cfg(), engine_available=lambda: True, run_loop=lambda *a, **k: None) is None


def test_the_thread_runs_the_loop_with_the_hosts_rental_settings_and_a_gate():
    from computecoin_agent.platform import use_platform
    from computecoin_agent.platform.fake import make_fake_platform

    seen = {}

    def loop(api, executor, *, agent):
        seen["agent"] = agent

    with use_platform(make_fake_platform()):
        t = sa.start_background_sessions(_cfg(allow_container_rental=True, rental_root="/srv/r"), engine_available=lambda: True, run_loop=loop,
                                         api=object(), executor=_ex())
    t.join(5)
    agent = seen["agent"]
    assert agent.rental.allowed is True and agent.rental.root == "/srv/r" and t.daemon is True
    assert agent.claim_gate() is True


def test_the_gate_follows_heat_and_the_owner():
    from computecoin_agent.platform import use_platform
    from computecoin_agent.platform.fake import make_fake_platform

    seen = {}
    with use_platform(make_fake_platform()):
        guard, owner = types.SimpleNamespace(may_claim=lambda: True), types.SimpleNamespace(availability=lambda: "online")
        t = sa.start_background_sessions(_cfg(), thermal_guard=guard, owner=owner, engine_available=lambda: True,
                                         run_loop=lambda api, ex, *, agent: seen.update(agent=agent), api=object(), executor=_ex())
    t.join(5)
    gate = seen["agent"].claim_gate
    assert gate() is True
    guard.may_claim = lambda: False
    assert gate() is False
    guard.may_claim = lambda: True
    owner.availability = lambda: "throttled"
    assert gate() is False


def test_a_crash_in_the_thread_is_logged_and_never_raised(caplog):
    from computecoin_agent.platform import use_platform
    from computecoin_agent.platform.fake import make_fake_platform

    def loop(*a, **k):
        raise RuntimeError("secret detail")

    with use_platform(make_fake_platform()), caplog.at_level("ERROR"):
        t = sa.start_background_sessions(_cfg(), engine_available=lambda: True, run_loop=loop, api=object(), executor=_ex())
        t.join(5)
    assert not t.is_alive() and "the session thread stopped: RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text  # only the kind of error is logged


# ============================================================================== files the container made, disk, scan caps
def test_everything_the_container_made_is_opened_up_before_it_is_removed_and_before_each_export(tmp_path):
    api = FakeApi(**{"/heartbeat": _beats(6, export_on=2)})
    runner = Runner(on_run=_plant({"a": b"1"}))
    ag = agent_for(tmp_path, api, runner, upload=fake_upload([]))
    run_serve(ag, _claim())
    unlock = ["docker", "exec", f"ccoin-sess-{SID}", "chmod", "-R", "a+rwX", "/workspace"]
    calls = runner.calls
    assert unlock in calls
    rm = [i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"]][-1]
    assert calls.index(unlock) < rm  # opened up while the container still existed
    results_only = ["docker", "exec", f"ccoin-sess-{SID}", "chmod", "-R", "a+rwX", "/workspace/results"]
    assert results_only in calls  # before the on-request export only the results folder is opened (a big dataset is not crawled)
    assert calls.index(results_only) < rm


def test_the_unlock_is_only_for_rentals_and_a_missing_chmod_is_harmless(tmp_path):
    runner = Runner()
    ag = agent_for(tmp_path, FakeApi(), runner)
    ag.stop_container({"id": "kit1", "image_digest": IMG})  # a Kit session: nothing to unlock
    assert not [c for c in runner.calls if "chmod" in c]
    ag2 = agent_for(tmp_path, FakeApi(), lambda argv: (_ for _ in ()).throw(RuntimeError("no chmod")))
    ag2._unlock_workspace(SID)  # must not raise


def test_a_workspace_that_will_not_delete_is_opened_up_by_a_throwaway_container_and_deleted(tmp_path, monkeypatch):
    runner = Runner()
    ag = agent_for(tmp_path, FakeApi(), runner)
    claim = _claim()
    ag.prepare_rental(claim)
    ws = ag._runs[SID].workspace
    deletes = []
    real_delete = rental_mod.Workspace.delete

    def stubborn_once(self):
        deletes.append(1)
        if len(deletes) > 1:
            real_delete(self)  # the second attempt, after the helper opened things up, works

    monkeypatch.setattr(rental_mod.Workspace, "delete", stubborn_once)
    ag.finish_rental(claim, export=False)
    helper = [c for c in runner.calls if c[:3] == ["docker", "run", "--rm"]]
    assert len(deletes) == 2 and len(helper) == 1
    argv = helper[0]
    assert argv[argv.index("--network") + 1] == "none" and argv[argv.index("--cap-drop") + 1] == "ALL" and "no-new-privileges" in argv
    assert argv[argv.index("--user") + 1] == "65534:65534" and argv[argv.index("--entrypoint") + 1] == "chmod" and IMG in argv
    assert f"type=bind,source={ws.path},target=/w" in argv
    assert not os.path.exists(ws.top)


def test_a_nearly_full_host_disk_stops_a_rental_even_inside_its_quota(tmp_path, monkeypatch):
    api = FakeApi(**{"/heartbeat": (200, {"stop": False})})
    monkeypatch.setattr(rental_mod.Workspace, "disk_low", lambda self: True)
    ag = agent_for(tmp_path, api, Runner(), upload=fake_upload([]))
    assert run_serve(ag, _claim()) == "stop"
    assert api.body("/ended") == {"failed": False}


def test_disk_low_reads_the_hosts_free_space_and_an_unreadable_disk_is_not_low(tmp_path, monkeypatch):
    ws = rental_mod.Workspace(str(tmp_path), SID, rental_mod.GIB)
    monkeypatch.setattr(rental_mod.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=100 * rental_mod.MIB))
    assert ws.disk_low() is True
    monkeypatch.setattr(rental_mod.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=5 * rental_mod.GIB))
    assert ws.disk_low() is False
    monkeypatch.setattr(rental_mod.shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError("gone")))
    assert ws.disk_low() is False


def test_a_results_folder_full_of_junk_stops_the_scan(tmp_path, monkeypatch):
    res = tmp_path / "results"
    res.mkdir()
    for i in range(20):
        (res / f"bad;{i}").write_text("x")  # names the rules refuse
    monkeypatch.setattr(rental_mod, "MAX_SCAN_SKIPS", 5)
    names, skipped, truncated = rental_mod.list_results(str(res))
    assert names == [] and truncated is True and skipped <= 7


def test_a_results_folder_swapped_for_a_link_leaks_nothing(tmp_path):
    secret = tmp_path / "hostdir"
    secret.mkdir()
    (secret / "secret.txt").write_text("TOP SECRET")
    run = _export_run(tmp_path, {})
    res = os.path.join(run.workspace.path, "results")
    os.rmdir(res)
    os.symlink(secret, res)  # the customer's code replaced `results` itself with a link to a host folder
    store = []
    out = rental_mod.export_results(FakeApi(), SID, run, upload=fake_upload(store))
    assert store == [] and out.files == []
