"""U10 session agent (written, NOT executed): E2E crypto and hardened argv."""

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from computecoin_agent import session_agent as sa
from computecoin_agent.executor import DockerExecutor

DIGEST = "img@sha256:" + "a" * 64


def _pair():
    host = X25519PrivateKey.generate()
    browser = X25519PrivateKey.generate()
    pub = sa._pub_hex(browser)
    agent = sa.SessionCrypto("sid1", host, pub)
    return host, browser, agent


def test_agent_seals_and_tamper_is_rejected():
    host, browser, agent = _pair()
    frame = agent.seal(sa.T_DATA, b"hello")
    assert b"hello" not in frame
    with pytest.raises(sa.SessionAgentError):
        agent.open(b"\x00" * 40)


def test_wrong_session_id_gives_different_keys():
    host = X25519PrivateKey.generate()
    pub = sa._pub_hex(X25519PrivateKey.generate())
    eph = X25519PrivateKey.generate()
    a = sa.SessionCrypto("s1", host, pub, ephemeral=eph).seal(0, b"m")
    b = sa.SessionCrypto("s2", host, pub, ephemeral=eph).seal(0, b"m")
    assert a != b


def _ex():
    return DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True)


def test_session_argv_is_hardened_and_publishes_nothing():
    claim = {"id": "abc", "image_digest": DIGEST, "gpu_count": 0, "max_duration_seconds": 600}
    argv = sa.build_session_argv(_ex(), claim, "/tmp/w")
    assert argv[0] == "docker" and "-d" in argv and "--name" in argv
    assert argv[argv.index("--network") + 1] == "none"
    for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--user"):
        assert flag in argv
    assert "-p" not in argv and "--publish" not in argv and "timeout" not in argv


def test_private_tier_refused():
    with pytest.raises(sa.SessionAgentError):
        sa.build_session_argv(_ex(), {"id": "a", "image_digest": DIGEST, "isolation_tier": "sealed"}, "/tmp/w")


def test_attach_argv():
    assert sa.build_attach_argv({"id": "a", "mode": "pty"})[:3] == ["docker", "exec", "-it"]
    assert sa.build_attach_argv({"id": "a", "mode": "tcp", "tcp_port": 8888})[-2:] == ["127.0.0.1", "8888"]


def test_executor_argv_parts_place_image_explicitly():
    from computecoin_agent.executor import JobSpec

    spec = JobSpec(job_id="j", workload_type="session", model="session", input={}, execution_mode="hardware",
                   image_digest=DIGEST, limits={"max_runtime_seconds": 60})
    prefix, image, suffix = _ex().build_argv_parts(spec, "/tmp/w")
    assert image == DIGEST and suffix == [] and DIGEST not in prefix
    assert _ex().build_argv(spec, "/tmp/w") == [*prefix, image, *suffix]


def test_tcp_session_without_command_puts_image_last():
    claim = {"id": "abc", "image_digest": DIGEST, "mode": "tcp", "tcp_port": 8888}
    argv = sa.build_session_argv(_ex(), claim, "/tmp/w")
    assert argv[-1] == DIGEST and "--entrypoint" not in argv
    assert any(a.startswith("COMPUTECOIN_SESSION_BASE_PATH=") for a in argv)


def test_tcp_session_command_override_after_image_flags_before():
    claim = {"id": "abc", "image_digest": DIGEST, "mode": "tcp", "tcp_port": 8888,
             "command": ["jupyter", "lab", "--no-browser"]}
    argv = sa.build_session_argv(_ex(), claim, "/tmp/w")
    k = argv.index(DIGEST)
    assert argv[k - 2:k] == ["--entrypoint", "jupyter"]
    assert argv[k + 1:] == ["lab", "--no-browser"]
    assert argv.index("-d") < k and argv.index("COMPUTECOIN_SESSION_BASE_PATH=" + sa.SESSION_BASE_PATH.format(sid="abc")) < k


def test_tcp_session_invalid_command_refused():
    for bad in (["", "x"], "jupyter", ["a\x00b"]):
        with pytest.raises(sa.SessionAgentError):
            sa.build_session_argv(_ex(), {"id": "a", "image_digest": DIGEST, "mode": "tcp", "command": bad}, "/tmp/w")


def test_unexpected_executor_parts_refused():
    class Weird(DockerExecutor):
        def build_argv_parts(self, spec, out):
            p, i, s = super().build_argv_parts(spec, out)
            return p, i, ["extra"]

    ex = Weird(docker_available=lambda: True, nvidia_available=lambda: True)
    with pytest.raises(sa.SessionAgentError):
        sa.build_session_argv(ex, {"id": "a", "image_digest": DIGEST}, "/tmp/w")


def test_session_container_never_binds_the_shared_work_dir(tmp_path):
    """run_forever() builds SessionAgent with the default workdir (the host's /tmp): the /out bind must be a private
    per-session dir, never the shared dir (which holds every other job's inputs and outputs)."""
    import os, types

    ran = []

    def runner(argv):
        ran.append(list(argv))
        return types.SimpleNamespace(returncode=0, stderr="")

    shared = tmp_path / "shared"
    shared.mkdir()
    agent = sa.SessionAgent(None, _ex(), runner=runner, workdir=str(shared))
    claim = {"id": "abc", "image_digest": DIGEST, "gpu_count": 0, "max_duration_seconds": 600}
    agent.start_container(claim)
    mount = next(a for a in ran[0] if a.startswith("type=bind,source=") and a.endswith("target=/out"))
    source = mount.split("source=", 1)[1].split(",", 1)[0]
    assert source != str(shared) and os.path.dirname(source) == str(shared) and os.path.isdir(source)
    assert os.listdir(source) == []
    agent.stop_container(claim)
    assert not os.path.exists(source)


# --- relay socket / heartbeat resilience (audit) -------------------------------------------------------------- #
class _FakeWs:
    """Async context manager + async iterator standing in for a websockets connection."""

    def __init__(self, *, raise_on_iter=None):
        import asyncio

        self._closed = asyncio.Event()
        self._raise = raise_on_iter
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        if self._raise is not None:
            raise self._raise
        await self._closed.wait()
        return
        yield  # pragma: no cover - makes this an async generator

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self._closed.set()


def _agent_with(api, ws_factory):
    agent = sa.SessionAgent(api, _ex(), ws_connect=ws_factory)
    agent.start_container = lambda claim: None
    agent.stop_container = lambda claim: None
    return agent


def test_heartbeat_survives_a_transient_network_error(monkeypatch):
    import asyncio

    monkeypatch.setattr(sa, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(sa, "HEARTBEAT_RETRY_SECONDS", 0.01)
    beats = []

    class Api:
        def relay_url(self, sid, token):
            return "wss://x"

        def post(self, path, body=None):
            if path.endswith("/heartbeat"):
                beats.append(path)
                if len(beats) == 1:
                    raise ConnectionError("blip")
                return 200, {"stop": len(beats) >= 3}
            return 200, {}

    ws = _FakeWs()
    agent = _agent_with(Api(), lambda url: ws)

    async def go():
        return await asyncio.wait_for(agent._serve_socket({"id": "s1"}, X25519PrivateKey.generate(), "t", 1e12), 5)

    assert asyncio.run(go()) == "stop"
    assert len(beats) >= 3  # the first failure did not kill the heartbeat task


def test_serve_reconnects_after_a_dropped_relay_socket(monkeypatch):
    import asyncio

    from websockets.exceptions import ConnectionClosedError

    monkeypatch.setattr(sa, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(sa.asyncio, "sleep", _fast_sleep(asyncio.sleep))
    sockets = []

    def connect(url):
        ws = _FakeWs(raise_on_iter=ConnectionClosedError(None, None)) if not sockets else _FakeWs()
        sockets.append(ws)
        return ws

    class Api:
        def relay_url(self, sid, token):
            return "wss://x"

        def post(self, path, body=None):
            if path.endswith("/token"):
                return 200, {"host_token": "t2"}
            if path.endswith("/heartbeat"):
                return 200, {"stop": len(sockets) >= 2}
            return 200, {}

    agent = _agent_with(Api(), connect)
    claim = {"id": "s1", "host_token": "t1", "max_duration_seconds": 3600}

    async def go():
        return await asyncio.wait_for(agent.serve(claim, X25519PrivateKey.generate()), 5)

    assert asyncio.run(go()) == "stop"
    assert len(sockets) == 2  # reconnected instead of failing the session


def _fast_sleep(real):
    async def _s(delay, *a, **k):
        return await real(min(delay, 0.01), *a, **k)

    return _s


def test_session_loop_survives_a_transient_claim_error():
    calls = []

    class Api:
        def post(self, path, body=None):
            calls.append(path)
            if len(calls) == 1:
                raise ConnectionError("blip")  # used to end `computecoin-agent sessions` with a traceback
            raise KeyboardInterrupt  # stop the endless loop for the test

    with pytest.raises(KeyboardInterrupt):
        sa.run_forever(Api(), _ex(), poll_seconds=0, sleeper=lambda s: None)
    assert len(calls) == 2


def test_session_loop_once_still_raises():
    class Api:
        def post(self, path, body=None):
            raise ConnectionError("blip")

    with pytest.raises(ConnectionError):
        sa.run_forever(Api(), _ex(), once=True, sleeper=lambda s: None)
