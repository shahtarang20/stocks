"""W4 control channel: protocol, authentication, caps and the runner-side drain / stop semantics.

Written, not run. Sockets are loopback or ``socket.socketpair`` only; no network, no sleeps beyond short joins.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from computecoin_agent import control_channel as cc
from computecoin_agent import jobrunner

TOKEN = "a" * 64
OTHER = "b" * 64


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------------- messages
def test_every_message_type_round_trips():
    samples = {
        "to_runner": {
            "start": {"max_concurrent_jobs": 2, "availability": "online"},
            "drain": {"reason": "x"},
            "stop": {"grace_seconds": 5},
            "set_limits": {"gpu_temp_cap_c": 80, "gpu_temp_resume_c": 70, "gpu_temp_hard_stop_c": 90, "gpu_power_limit_w": None},
            "set_availability": {"availability": "yielding", "gpu_temp_c": 61.5},
        },
        "to_supervisor": {
            "ready": {"agent_version": "1", "protocol": 1, "state": "waiting"},
            "job_started": {"job_id": "j1", "active_jobs": 1},
            "job_finished": {"job_id": "j1", "status": "success", "active_jobs": 0, "draining": False},
            "error": {"code": "bad", "message": "m", "ref_seq": 3},
            "usage": {"active_jobs": 0, "draining": True, "availability": "online", "started": True, "stopped": False},
        },
    }
    for direction, msgs in samples.items():
        for t, data in msgs.items():
            raw = cc.encode_message(t, data, 7, direction=direction)
            msg = cc.decode_message(raw, direction=direction)
            assert (msg.type, msg.data, msg.seq, msg.v) == (t, data, 7, 1)


@pytest.mark.parametrize("t,data", [
    ("start", {"bogus": 1}),                                # unknown field
    ("set_availability", {}),                               # missing required
    ("set_availability", {"availability": "asleep"}),       # not an allowed state
    ("set_limits", {}),                                     # empty
    ("set_limits", {"gpu_temp_cap_c": True}),               # bool is not a number
    ("set_limits", {"max_concurrent_jobs": 1000}),          # out of range
    ("stop", {"grace_seconds": -1}),
    ("drain", {"reason": "x" * 201}),
])
def test_invalid_commands_are_refused(t, data):
    with pytest.raises(cc.ProtocolError):
        cc.encode_message(t, data, 0, direction="to_runner")


def test_a_type_is_only_valid_in_its_own_direction():
    with pytest.raises(cc.ProtocolError):
        cc.encode_message("ready", {"agent_version": "1", "protocol": 1, "state": "x"}, 0, direction="to_runner")
    raw = cc.encode_message("drain", {}, 0, direction="to_runner")
    with pytest.raises(cc.ProtocolError):
        cc.decode_message(raw, direction="to_supervisor")


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"v":1}', b'{"v":2,"seq":0,"type":"drain","data":{}}',
                                 b'{"v":1,"seq":0,"type":"drain","data":{},"extra":1}', b'\xff\xfe'])
def test_malformed_envelopes_are_refused(raw):
    with pytest.raises(cc.ProtocolError):
        cc.decode_message(raw, direction="to_runner")


def test_oversize_message_is_refused_on_both_sides():
    with pytest.raises(cc.ProtocolError):
        cc.decode_message(b" " * (cc.MAX_FRAME_BYTES + 1), direction="to_runner")
    with pytest.raises(cc.ProtocolError):
        cc.encode_message("error", {"code": "c" * 65}, 0, direction="to_supervisor")  # field cap


# --------------------------------------------------------------------------- addresses and tokens
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "8.8.8.8", "192.168.1.5", "224.0.0.1", ""])
def test_non_loopback_addresses_are_refused_by_default(host):
    with pytest.raises(ValueError):
        cc.validate_address(host)


def test_loopback_and_explicit_private_addresses():
    assert cc.validate_address("127.0.0.1") == "127.0.0.1"
    assert cc.validate_address("::1") == "::1"
    assert cc.validate_address("172.20.0.2", allow_private=True) == "172.20.0.2"
    with pytest.raises(ValueError):
        cc.validate_address("8.8.8.8", allow_private=True)
    with pytest.raises(ValueError):
        cc.validate_address("0.0.0.0", allow_private=True)


def test_server_constructor_refuses_a_lan_bind():
    with pytest.raises(ValueError):
        cc.ControlServer("0.0.0.0", 0, TOKEN, lambda m: None)


def test_parse_hostport():
    assert cc.parse_hostport("127.0.0.1:47821") == ("127.0.0.1", 47821)
    assert cc.parse_hostport("[::1]:9") == ("::1", 9)
    with pytest.raises(ValueError):
        cc.parse_hostport("nonsense")


def test_short_token_is_refused():
    with pytest.raises(cc.AuthError):
        cc.ControlServer("127.0.0.1", 0, "short", lambda m: None)


def test_load_token_prefers_file_and_refuses_group_readable(tmp_path):
    import os
    f = tmp_path / "t"
    f.write_text(TOKEN + "\n")
    os.chmod(f, 0o600) if os.name == "posix" else None
    assert cc.load_token(str(f)) == TOKEN
    if os.name == "posix":
        os.chmod(f, 0o644)
        with pytest.raises(cc.AuthError):
            cc.load_token(str(f))
    assert cc.load_token(None, env={cc.TOKEN_ENV: OTHER}) == OTHER
    with pytest.raises(cc.AuthError):
        cc.load_token(None, env={})


# --------------------------------------------------------------------------- handshake over a socketpair
def _handshake_pair(server_token: str, client_token: str):
    a, b = socket.socketpair()
    out: dict = {}

    def srv():
        try:
            out["server"] = cc.server_handshake(a, server_token)
        except Exception as exc:  # noqa: BLE001
            out["server_err"] = exc
            a.close()

    t = threading.Thread(target=srv)
    t.start()
    try:
        out["client"] = cc.client_handshake(b, client_token)
    except Exception as exc:  # noqa: BLE001
        out["client_err"] = exc
        b.close()
    t.join(5)
    return out


def test_handshake_and_message_exchange():
    out = _handshake_pair(TOKEN, TOKEN)
    server, client = out["server"], out["client"]
    client.send("drain", {"reason": "r"})
    assert server.recv().type == "drain"
    server.send("usage", {"active_jobs": 0, "draining": True, "availability": "online", "started": True, "stopped": False})
    assert client.recv().data["draining"] is True


def test_wrong_token_fails_on_both_sides_with_no_session():
    out = _handshake_pair(TOKEN, OTHER)
    assert isinstance(out.get("server_err"), cc.AuthError)
    assert isinstance(out.get("client_err"), cc.AuthError)
    assert "server" not in out and "client" not in out


def test_tampered_frame_is_rejected():
    out = _handshake_pair(TOKEN, TOKEN)
    server, client = out["server"], out["client"]
    raw = cc.encode_message("drain", {}, 0, direction="to_runner")
    # a frame with a valid layout but a MAC computed with a different key (a stream injector)
    bad = raw + b"\x00" * cc.MAC_LEN
    client._sock.sendall(struct.pack(">I", len(bad)) + bad)
    with pytest.raises(cc.ProtocolError):
        server.recv()


class _RecordingSocket:
    """socket.socket has __slots__, so wrap it instead of patching sendall on the instance."""

    def __init__(self, sock):
        self._s = sock
        self.sent: list[bytes] = []

    def sendall(self, data):
        self.sent.append(bytes(data))
        self._s.sendall(data)

    def __getattr__(self, name):
        return getattr(self._s, name)


def test_replayed_frame_is_rejected():
    out = _handshake_pair(TOKEN, TOKEN)
    server, client = out["server"], out["client"]
    rec = _RecordingSocket(client._sock)
    client._sock = rec
    client.send("drain", {})
    assert server.recv().type == "drain"
    rec.sendall(rec.sent[0])  # the same bytes again
    with pytest.raises(cc.ProtocolError):
        server.recv()


def test_oversize_frame_is_rejected_before_the_body_is_read():
    out = _handshake_pair(TOKEN, TOKEN)
    server, client = out["server"], out["client"]
    client._sock.sendall(struct.pack(">I", cc.MAX_FRAME_BYTES + cc.MAC_LEN + 1))  # header only, no body ever sent
    with pytest.raises(cc.ProtocolError):
        server.recv()


def test_oversize_handshake_frame_is_rejected():
    a, b = socket.socketpair()
    b.sendall(struct.pack(">I", cc.MAX_HANDSHAKE_BYTES + 1))
    with pytest.raises(cc.ProtocolError):
        cc._read_plain(a, cc.MAX_HANDSHAKE_BYTES)


# --------------------------------------------------------------------------- server over real loopback
def _server(handler=None, **kw):
    s = cc.ControlServer("127.0.0.1", 0, TOKEN, handler or (lambda m: None), **kw)
    port = s.start()
    return s, port


def _connect(port, token=TOKEN, on_message=lambda m: None):
    c = cc.ControlClient("127.0.0.1", port, token, on_message)
    c.connect()
    return c


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_server_binds_loopback_only_and_delivers_messages():
    got: list = []
    server, port = _server(got.append)
    try:
        assert server._lsock.getsockname()[0] == "127.0.0.1"
        client = _connect(port)
        client.send("set_availability", {"availability": "draining"})
        assert _wait(lambda: got)
        assert got[0].type == "set_availability"
        client.close()
    finally:
        server.close()


def test_wrong_token_client_is_refused_and_counted_toward_lockout():
    clock = Clock()
    server, port = _server(clock=clock, max_auth_failures=3, lockout_s=30.0)
    try:
        for _ in range(3):
            with pytest.raises(cc.ControlChannelError):
                _connect(port, token=OTHER)
        assert _wait(lambda: server._clock() < server._locked_until)
        # now even the right token is refused until the lockout passes
        with pytest.raises((cc.ControlChannelError, OSError)):
            _connect(port)
        clock.t += 31
        c = _connect(port)
        assert server.connected() or _wait(server.connected)
        c.close()
    finally:
        server.close()


def test_connection_rate_cap():
    clock = Clock()
    server, port = _server(clock=clock, max_connections_per_window=2)
    try:
        refused = 0
        for _ in range(5):
            try:
                c = _connect(port)
                c.close()
            except (cc.ControlChannelError, OSError):
                refused += 1
        assert refused >= 1
    finally:
        server.close()


def test_message_rate_cap_closes_the_session():
    closed = threading.Event()
    clock = Clock()
    server, port = _server(clock=clock, max_messages_per_second=1.0, message_burst=3, on_disconnect=closed.set)
    try:
        client = _connect(port)
        for _ in range(10):
            try:
                client.send("drain", {})
            except Exception:  # noqa: BLE001
                break
        assert closed.wait(3)
    finally:
        server.close()


def test_new_authenticated_session_replaces_the_old_one():
    server, port = _server()
    try:
        first = _connect(port)
        assert _wait(server.connected)
        second = _connect(port)
        assert _wait(lambda: not first.is_open())
        assert second.is_open()
        second.close()
    finally:
        server.close()


# --------------------------------------------------------------------------- RunnerControl semantics (fakes)
class FakeServer:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def send(self, t, d=None):
        self.sent.append((t, d or {}))
        return True


class FakeGuard:
    cap_c, resume_c, hard_stop_c = 85.0, 80.0, 95.0


def _rc(**kw):
    clock = kw.pop("clock", Clock())
    rc = cc.RunnerControl(clock=clock, **kw)
    srv = FakeServer()
    rc.attach(srv)
    return rc, srv, clock


def _cmd(rc, t, data=None, seq=1):
    rc.handle(cc.ControlMessage(type=t, data=data or {}, seq=seq))


def test_no_claim_before_start():
    rc, _srv, _ = _rc()
    assert rc.claim_block_reason() == "waiting_for_start"
    _cmd(rc, "start")
    assert rc.may_claim()


def test_drain_finishes_current_job_then_stops_claiming():
    rc, srv, _ = _rc()
    _cmd(rc, "start")
    rc.job_started("j1")
    _cmd(rc, "drain")
    assert rc.claim_block_reason() == "draining"
    assert rc.snapshot()["active_jobs"] == 1          # the job is NOT cancelled
    rc.job_finished("j1", "success")
    assert rc.snapshot()["active_jobs"] == 0 and not rc.may_claim()
    last = [d for t, d in srv.sent if t == "job_finished"][-1]
    assert last["draining"] is True and last["active_jobs"] == 0
    _cmd(rc, "start")                                   # a fresh start clears the drain
    assert rc.may_claim()


def test_stop_ends_the_loop_only_when_idle():
    stopped = []
    rc, _srv, _ = _rc(on_stop=lambda: stopped.append(1))
    _cmd(rc, "start")
    rc.job_started("j1")
    _cmd(rc, "stop")
    assert stopped and not rc.may_claim() and not rc.should_stop()   # a job is still running
    rc.job_finished("j1", "success")
    assert rc.should_stop()
    _cmd(rc, "start")                                   # cannot be restarted once stopped
    assert not rc.may_claim()


def test_availability_other_than_online_blocks_claims_and_is_reversible():
    rc, _srv, _ = _rc()
    _cmd(rc, "start")
    for state in ("throttled", "yielding", "maintenance", "draining"):
        _cmd(rc, "set_availability", {"availability": state})
        assert rc.claim_block_reason() == state
    _cmd(rc, "set_availability", {"availability": "online"})
    assert rc.may_claim()


def test_concurrency_limit():
    rc, _srv, _ = _rc()
    _cmd(rc, "start")
    rc.job_started("a")
    assert rc.claim_block_reason() == "at_capacity"
    _cmd(rc, "set_limits", {"max_concurrent_jobs": 2})
    assert rc.may_claim()
    _cmd(rc, "set_limits", {"max_concurrent_jobs": 0})
    assert not rc.may_claim()


def test_invalid_thermal_limits_change_nothing_and_report_an_error():
    rc, srv, _ = _rc()
    guard = FakeGuard()
    rc.bind_thermal_guard(guard)
    _cmd(rc, "set_limits", {"gpu_temp_cap_c": 70, "gpu_temp_resume_c": 75}, seq=9)
    assert (guard.cap_c, guard.resume_c, guard.hard_stop_c) == (85.0, 80.0, 95.0)
    err = [d for t, d in srv.sent if t == "error"][-1]
    assert err["code"] == "invalid_limits" and err["ref_seq"] == 9
    _cmd(rc, "set_limits", {"gpu_temp_cap_c": 82, "gpu_temp_resume_c": 75})
    assert (guard.cap_c, guard.resume_c) == (82.0, 75.0)


def test_thermal_limits_without_a_guard_are_refused():
    rc, srv, _ = _rc()
    _cmd(rc, "set_limits", {"gpu_temp_cap_c": 82})
    assert [d for t, d in srv.sent if t == "error"][-1]["code"] == "no_thermal_guard"


def test_supervisor_temperature_feeds_the_sampler_and_goes_stale():
    rc, _srv, clock = _rc()
    assert rc.temperature_sampler() == []
    _cmd(rc, "set_availability", {"availability": "online", "gpu_temp_c": 77.0})
    assert [r.temp_c for r in rc.temperature_sampler()] == [77.0]
    clock.t += cc.RunnerControl.TEMP_STALE_SECONDS + 1
    assert rc.temperature_sampler() == []                 # stale reading never blocks


def test_orphaned_runner_stops_claiming_after_the_grace_but_keeps_its_job():
    rc, _srv, clock = _rc(orphan_grace_seconds=60.0)
    rc.on_connect()
    _cmd(rc, "start")
    rc.job_started("j")
    rc.on_disconnect()
    assert rc.snapshot()["active_jobs"] == 1
    clock.t += 30
    rc.job_finished("j", "success")
    assert rc.may_claim()
    clock.t += 31
    assert rc.claim_block_reason() == "supervisor_gone"


def test_abort_active_reports_failures():
    rc, srv, _ = _rc()
    _cmd(rc, "start")
    rc.job_started("j")
    rc.abort_active()
    assert rc.snapshot()["active_jobs"] == 0 and rc.snapshot()["jobs_failed"] == 1
    assert [d for t, d in srv.sent if t == "job_finished"][-1]["status"] == "failed"


# --------------------------------------------------------------------------- poll_loop hook
def test_poll_loop_without_control_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(jobrunner, "run_one", lambda *a, **k: (calls.append(k), {"claimed": False})[1])
    jobrunner.poll_loop("http://x", "m", "s", iterations=2, sleeper=lambda s: None)
    assert len(calls) == 2 and all("control" not in k for k in calls)


def test_poll_loop_obeys_start_drain_and_stop(monkeypatch):
    rc, srv, _ = _rc()
    seen = []

    def fake_run_one(base, mid, sec, **kw):
        ctl = kw["control"]
        ctl.job_started("job-1")
        seen.append("ran")
        return {"claimed": True, "job_id": "job-1", "status": "success"}

    monkeypatch.setattr(jobrunner, "run_one", fake_run_one)
    summaries = jobrunner.poll_loop("http://x", "m", "s", iterations=2, sleeper=lambda s: None, control=rc)
    assert [s.get("held") for s in summaries] == ["waiting_for_start"] * 2 and not seen   # nothing before start
    _cmd(rc, "start")
    _cmd(rc, "drain")
    held = jobrunner.poll_loop("http://x", "m", "s", iterations=1, sleeper=lambda s: None, control=rc)
    assert held[0]["held"] == "draining" and not seen
    _cmd(rc, "start")
    out = jobrunner.poll_loop("http://x", "m", "s", iterations=1, sleeper=lambda s: None, control=rc)
    assert seen == ["ran"] and out[0]["status"] == "success"
    assert ("job_started", {"job_id": "job-1", "active_jobs": 1}) in srv.sent
    assert [d["status"] for t, d in srv.sent if t == "job_finished"] == ["success"]
    _cmd(rc, "stop")
    assert jobrunner.poll_loop("http://x", "m", "s", iterations=5, sleeper=lambda s: None, control=rc) == []


def test_poll_loop_reports_a_job_that_raised_as_failed(monkeypatch):
    rc, srv, _ = _rc()
    _cmd(rc, "start")

    def boom(base, mid, sec, **kw):
        kw["control"].job_started("j")
        raise RuntimeError("executor blew up")

    monkeypatch.setattr(jobrunner, "run_one", boom)
    out = jobrunner.poll_loop("http://x", "m", "s", iterations=1, sleeper=lambda s: None, control=rc)
    assert "error" in out[0] and rc.snapshot()["active_jobs"] == 0
    assert [d["status"] for t, d in srv.sent if t == "job_finished"] == ["failed"]
