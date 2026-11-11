"""W2 supervisor loop with fakes: availability, heartbeat mapping, native telemetry, runner lifecycle, drain and stop.

No Windows, no wsl.exe, no network, no threads started (``tick`` and ``heartbeat_once`` are driven directly). Written, not run.
"""

from __future__ import annotations

import types

import pytest

from computecoin_agent import control_channel as cc
from computecoin_agent.heartbeat import HeartbeatAuthError
from computecoin_agent.thermal import ThermalGuard
from computecoin_agent.windows import supervisor as sv

TOKEN = "c" * 64


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeProc:
    def __init__(self, exits_on_stop=True):
        self.code = None
        self.terminated = self.killed = False
        self.exits_on_stop = exits_on_stop

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def kill(self):
        self.killed = True
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class FakeLauncher:
    def __init__(self):
        self.procs: list[FakeProc] = []
        self.fail = False
        self.container_cleanups = 0

    def stop_job_containers(self):
        self.container_cleanups += 1

    def launch(self):
        if self.fail:
            raise OSError("wsl.exe missing")
        p = FakeProc()
        self.procs.append(p)
        return p


class FakeClient:
    """Stands in for ControlClient. ``ready_on_connect`` mimics the runner's `ready` arriving."""

    instances: list["FakeClient"] = []

    def __init__(self, host, port, token, on_message, *, allow_private=False, on_close=None):
        self.host, self.port, self.token, self.on_message, self.on_close = host, port, token, on_message, on_close
        self.sent: list[tuple[str, dict]] = []
        self.open = False
        FakeClient.instances.append(self)

    def connect(self):
        self.open = True
        self.on_message(cc.ControlMessage("ready", {"agent_version": "1", "protocol": 1, "state": "waiting"}))

    def is_open(self):
        return self.open

    def send(self, t, d=None):
        self.sent.append((t, d or {}))
        return len(self.sent)

    def close(self):
        self.open = False


def _sample(temp=60.0, util=10.0, mem=1000, mode=None):
    return types.SimpleNamespace(temp_c=temp, gpu_util_pct=util, mem_used_mb=mem, extra_metrics={"mode": mode} if mode else None)


def make(settings=None, samples=None, source=None, guard=None, hb=None, **kw):
    FakeClient.instances = []
    clock = Clock()
    launcher = FakeLauncher()
    sent_heartbeats: list[dict] = []

    def heartbeat(base, mid, secret, **k):
        sent_heartbeats.append(k)
        if hb:
            hb()
        return types.SimpleNamespace(status_code=200)

    sup = sv.Supervisor(
        base_url="https://api.example", machine_id="mch_1", machine_secret="msk_" + "x" * 40, token=TOKEN,
        settings=settings or sv.SupervisorSettings(), launcher=launcher, report=object(),
        telemetry_sampler=lambda: list(samples if samples is not None else [_sample()]),
        availability_source=source, thermal_guard=guard, heartbeat_send=heartbeat,
        health_base=lambda report: {"uptime_s": 1}, client_factory=FakeClient, clock=clock, sleeper=lambda s: clock.advance(s),
        **kw,
    )
    sup.sent = sent_heartbeats
    sup.clock = clock
    sup.launcher = launcher
    return sup


def online(sup):
    """Drive ticks until the runner is launched, connected, started."""
    for _ in range(4):
        sup.tick()
    assert sup.availability() == "online", sup.snapshot()


# --------------------------------------------------------------------------- availability
def test_never_online_without_a_connected_started_runner():
    sup = make()
    sup.tick()
    assert sup.availability() == "throttled" and sup.snapshot()["detail"]["code"] == "runner_not_ready"
    assert sup.heartbeat_availability() == "throttled"
    online(sup)
    assert sup.heartbeat_availability() == "online"


def test_start_is_sent_with_limits_after_ready():
    guard = ThermalGuard(85, 80, 95, sampler=lambda: [])
    sup = make(guard=guard)
    online(sup)
    sent = FakeClient.instances[-1].sent
    kinds = [t for t, _ in sent]
    assert kinds.index("set_limits") < kinds.index("start")
    assert dict(sent)["set_limits"] == {"gpu_temp_cap_c": 85, "gpu_temp_resume_c": 80, "gpu_temp_hard_stop_c": 95}


@pytest.mark.parametrize("external,expected", [("yielding", "yielding"), ("maintenance", "maintenance"), ("draining", "draining")])
def test_external_states_override_online_and_map_to_throttled_in_the_heartbeat(external, expected):
    state = {"v": "online"}
    sup = make(source=lambda: (state["v"], {"code": "owner_active"}))
    online(sup)
    state["v"] = external
    sup.tick()
    assert sup.availability() == expected
    assert sup.heartbeat_availability() == "throttled"                     # never "maintenance" to the API
    sup.heartbeat_once()
    body = sup.sent[-1]
    assert body["availability"] == "throttled"
    assert body["health"]["availability_detail"] == {"state": expected, "code": "owner_active", "reason": "owner_active"}
    state["v"] = "online"
    sup.tick()
    assert sup.availability() == "online"


def test_precedence_draining_over_maintenance_over_yielding_over_throttled():
    guard = ThermalGuard(85, 80, 95, sampler=lambda: [_sample(90.0)])
    sup = make(source=lambda: ("yielding", {"code": "fullscreen"}), guard=guard, samples=[_sample(90.0)])
    online_runner = lambda: [sup.tick() for _ in range(4)]  # noqa: E731
    online_runner()
    assert sup.availability() == "yielding"                                # beats throttled
    sup.request_drain()
    sup.tick()
    assert sup.availability() == "draining"                                # beats yielding


def test_a_failing_availability_source_blocks_instead_of_allowing():
    def broken():
        raise RuntimeError("probe crashed")

    sup = make(source=broken)
    for _ in range(4):
        sup.tick()
    assert sup.availability() == "yielding"


def test_thermal_cap_makes_the_host_throttled_and_cooling_resumes():
    temps = {"t": 60.0}
    sup = make()
    guard = ThermalGuard(85, 80, 95, sampler=lambda: [_sample(temps["t"])])
    sup.set_thermal_guard(guard)
    online(sup)
    temps["t"] = 86.0
    sup.tick()
    assert sup.availability() == "throttled" and sup.snapshot()["detail"]["code"] == "gpu_throttled"
    temps["t"] = 82.0
    sup.tick()
    assert sup.availability() == "throttled"                               # hysteresis
    temps["t"] = 70.0
    sup.tick()
    assert sup.availability() == "online"


def test_native_temperature_and_availability_are_pushed_to_the_runner():
    sup = make(samples=[_sample(temp=71.5)])
    online(sup)
    sup.clock.advance(1)
    sup.tick()
    last = [d for t, d in FakeClient.instances[-1].sent if t == "set_availability"][-1]
    assert last == {"availability": "online", "gpu_temp_c": 71.5}


def test_stale_telemetry_is_not_forwarded_or_used():
    sup = make(samples=[_sample(temp=71.5)])
    online(sup)
    sup.clock.advance(sup._s.telemetry_max_age_seconds + 1)
    assert sup.thermal_sampler() == []


# --------------------------------------------------------------------------- telemetry honesty
def test_simulated_samples_are_dropped_not_reported_as_real(monkeypatch):
    from computecoin_agent import telemetry

    fake = [_sample(48.5, mode="simulated_probe")]
    monkeypatch.setattr(telemetry, "sample_gpu_metrics", lambda **k: fake)
    assert sv.native_gpu_sampler() == []
    real = [_sample(55.0)]
    monkeypatch.setattr(telemetry, "sample_gpu_metrics", lambda **k: real)
    assert sv.native_gpu_sampler() == real


def test_health_from_samples_takes_hottest_busiest_and_total_vram():
    h = sv.health_from_samples([_sample(60, 10, 1000), _sample(70, 90, 2000)])
    assert h == {"gpu_temp_c": 70, "gpu_util_pct": 90, "mem_used_mb": 3000}
    assert sv.health_from_samples([]) == {}


def test_heartbeat_health_carries_native_telemetry_and_detail():
    sup = make(samples=[_sample(66.0, 40.0, 2048)])
    online(sup)
    sup.heartbeat_once()
    health = sup.sent[-1]["health"]
    assert health["gpu_temp_c"] == 66.0 and health["gpu_util_pct"] == 40.0 and health["mem_used_mb"] == 2048
    assert health["uptime_s"] == 1 and health["availability_detail"]["state"] == "online"
    assert sup.sent[-1]["availability"] == "online"


# --------------------------------------------------------------------------- runner lifecycle
def test_runner_crash_restarts_with_backoff_then_gives_up():
    st = sv.SupervisorSettings(max_restarts=2, restart_backoff_seconds=(2.0, 5.0))
    sup = make(settings=st)
    online(sup)
    for expected_launches in (2, 3):
        sup.launcher.procs[-1].code = 1                                    # the runner dies
        sup.tick()                                                         # notices; waits out the backoff
        assert sup.availability() == "throttled" and len(sup.launcher.procs) == expected_launches - 1
        sup.clock.advance(6)
        sup.tick()
        assert len(sup.launcher.procs) == expected_launches
    sup.launcher.procs[-1].code = 1
    for _ in range(4):
        sup.clock.advance(10)
        sup.tick()
    assert sup.snapshot()["runner_gave_up"] and sup.availability() == "maintenance"
    assert len(sup.launcher.procs) == 3                                    # no further launches


def test_launch_failure_is_survived_and_retried():
    sup = make()
    sup.launcher.fail = True
    sup.tick()
    assert sup.availability() == "throttled" and not sup.launcher.procs
    sup.launcher.fail = False
    sup.clock.advance(10)
    sup.tick()
    assert len(sup.launcher.procs) == 1


def test_token_is_provisioned_before_every_launch_and_never_logged(caplog):
    seen = []
    sup = make(token_provisioner=seen.append)
    with caplog.at_level("DEBUG"):
        online(sup)
    assert seen == [TOKEN]
    assert TOKEN not in caplog.text


def test_runner_messages_update_state():
    sup = make()
    online(sup)
    sup._on_message(cc.ControlMessage("job_started", {"job_id": "j", "active_jobs": 1}))
    assert sup.snapshot()["active_jobs"] == 1
    sup._on_message(cc.ControlMessage("error", {"code": "invalid_limits"}))
    assert sup.last_error_codes == ["invalid_limits"]


# --------------------------------------------------------------------------- drain and stop
def test_shutdown_drains_then_stops_then_leaves_the_process_alone_when_it_exits():
    sup = make()
    online(sup)
    client = FakeClient.instances[-1]
    sup._on_message(cc.ControlMessage("job_started", {"job_id": "j", "active_jobs": 1}))

    def progress():
        # the job ends after a little while; the runner then exits when it gets `stop`
        if sup.clock.t > 1001 and sup._active_jobs():
            sup._on_message(cc.ControlMessage("job_finished", {"job_id": "j", "status": "success", "active_jobs": 0, "draining": True}))
        if any(t == "stop" for t, _ in client.sent):
            sup._proc.code = 0

    res = sup.shutdown(timeout=25.0, progress=progress)
    kinds = [t for t, _ in client.sent]
    assert kinds.index("drain") < kinds.index("stop")
    assert res.drained and not res.forced
    assert not sup.launcher.procs[0].terminated
    assert sup.sent[-1]["availability"] == "throttled" and sup.sent[-1]["health"]["availability_detail"]["code"] == "stopping"


def test_shutdown_never_cancels_the_job_early_but_gives_up_at_the_grace_and_forces_the_process():
    st = sv.SupervisorSettings(drain_grace_seconds=4.0, stop_grace_seconds=3.0)
    sup = make(settings=st)
    online(sup)
    sup._on_message(cc.ControlMessage("job_started", {"job_id": "j", "active_jobs": 1}))
    client = FakeClient.instances[-1]
    start = sup.clock.t
    res = sup.shutdown(timeout=25.0)
    assert not res.drained and res.forced and "lease" in res.note
    assert sup.launcher.procs[0].terminated
    assert [t for t, _ in client.sent].count("stop") == 1
    assert sup.clock.t - start <= 25.0 + 1                                  # honours the overall timeout


def test_shutdown_never_exceeds_half_the_timeout_for_draining():
    st = sv.SupervisorSettings(drain_grace_seconds=100.0, stop_grace_seconds=100.0)
    sup = make(settings=st)
    online(sup)
    sup._on_message(cc.ControlMessage("job_started", {"job_id": "j", "active_jobs": 1}))
    start = sup.clock.t
    sup.shutdown(timeout=10.0)
    assert sup.clock.t - start <= 10.0 + 1


def test_shutdown_survives_a_dead_runner_and_a_failing_final_heartbeat():
    def boom():
        raise OSError("network down")

    sup = make(hb=None)
    online(sup)
    sup._heartbeat_send = lambda *a, **k: boom()
    sup.launcher.procs[0].code = 1
    res = sup.shutdown(timeout=5.0)
    assert res.drained


def test_request_drain_is_visible_to_the_scheduler_and_resume_clears_it():
    sup = make()
    online(sup)
    sup.request_drain()
    sup.tick()
    assert sup.availability() == "draining" and ("drain", {"reason": "requested"}) in FakeClient.instances[-1].sent
    sup.resume()
    for _ in range(3):
        sup.tick()
    assert sup.availability() == "online"
    assert [t for t, _ in FakeClient.instances[-1].sent].count("start") == 2   # re-sent so the runner clears its drain


# --------------------------------------------------------------------------- heartbeat failures
def test_heartbeat_auth_failure_is_fatal_and_stops_the_loop():
    def reject():
        raise HeartbeatAuthError("401")

    sup = make(hb=reject)
    sup._heartbeat_loop()
    assert isinstance(sup.fatal_error(), HeartbeatAuthError) and sup._stop.is_set()


def test_transient_heartbeat_error_is_not_fatal():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("network")
        sup._stop.set()

    sup = make(hb=flaky, heartbeat_interval=0.0)
    sup._heartbeat_loop()
    assert sup.fatal_error() is None and calls["n"] == 2


# --------------------------------------------------------------------------- settings and launcher argv
def test_settings_reject_unknown_keys_and_bad_values():
    with pytest.raises(ValueError):
        sv.SupervisorSettings.from_dict({"control_prot": 1})
    with pytest.raises(ValueError):
        sv.SupervisorSettings.from_dict({"control_port": 0})
    assert sv.SupervisorSettings.from_dict({"control_port": 5000, "restart_backoff_seconds": [1, 2]}).restart_backoff_seconds == (1.0, 2.0)


def test_settings_load_missing_file_gives_defaults(tmp_path):
    assert sv.SupervisorSettings.load(tmp_path / "nope.toml") == sv.SupervisorSettings()


def test_runner_launch_argv_is_a_wsl_exec_list_without_the_token_or_a_shell():
    st = sv.SupervisorSettings()
    argv = sv.WslRunnerLauncher(st).runner_argv()
    assert isinstance(argv, list) and argv[0] == "wsl.exe" and "--exec" in argv
    assert argv[argv.index("--distribution") + 1] == st.distro and argv[argv.index("--user") + 1] == st.runner_user
    assert "--control-socket" in argv and f"127.0.0.1:{st.control_port}" in argv
    assert "--control-token-file" in argv
    assert not any("sh" == a.rsplit("/", 1)[-1] for a in argv)
    assert "--control-allow-private" not in argv


def test_launch_uses_popen_without_a_shell():
    calls = []
    launcher = sv.WslRunnerLauncher(sv.SupervisorSettings(), popen=lambda argv, **kw: (calls.append((argv, kw)), FakeProc())[1])
    launcher.launch()
    argv, kw = calls[0]
    assert kw["shell"] is False and isinstance(argv, list)


def test_token_provisioning_writes_via_stdin_to_a_preexisting_mode_600_file_and_never_in_argv():
    calls = []

    def run(argv, stdin=None):
        calls.append((list(argv), stdin))
        return types.SimpleNamespace(returncode=0)

    sv.WslRunnerLauncher(sv.SupervisorSettings(), run=run).provision_token(TOKEN)
    assert all(TOKEN not in " ".join(a) for a, _ in calls)
    assert calls[-1][1] == TOKEN.encode() and "tee" in calls[-1][0]
    install = [a for a, _ in calls if "install" in a][0]
    assert "600" in install and install.index("install") < len(install)
    assert calls.index(([a for a, _ in calls if "install" in a][0], None)) < len(calls) - 1  # file exists (empty, 600) before the secret is written


def test_token_provisioning_failure_is_an_error_not_a_silent_skip():
    launcher = sv.WslRunnerLauncher(sv.SupervisorSettings(), run=lambda argv, stdin=None: types.SimpleNamespace(returncode=1))
    with pytest.raises(cc.ControlChannelError):
        launcher.provision_token(TOKEN)


class MemStore:
    def __init__(self):
        self.d = {}

    def load(self, n):
        return self.d.get(n)

    def store(self, n, v):
        self.d[n] = v


def test_control_token_is_created_once_and_reused():
    store = MemStore()
    t1 = sv.ensure_control_token(store)
    assert len(t1) >= cc.MIN_TOKEN_CHARS and sv.ensure_control_token(store) == t1


def test_terminating_the_runner_also_removes_the_job_containers():
    sup = make()
    sup._proc = FakeProc()
    sup._terminate_process()
    assert sup.launcher.container_cleanups == 1
    assert sup._proc.terminated


def test_a_failing_container_cleanup_does_not_break_termination():
    sup = make()
    sup._proc = FakeProc()

    def boom():
        raise RuntimeError("docker unreachable")

    sup.launcher.stop_job_containers = boom
    sup._terminate_process()  # must not raise
    assert sup._proc.terminated


def test_launcher_removes_only_matching_containers_through_argv_without_a_shell():
    calls = []

    def run(argv, stdin=None):
        calls.append(list(argv))
        if "ps" in argv:
            return types.SimpleNamespace(returncode=0, stdout="0123456789ab\nnot-an-id; rm -rf /\nfedcba987654\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    sv.WslRunnerLauncher(sv.SupervisorSettings(), run=run).stop_job_containers()
    listing, removal = calls
    assert "name=ccoin-job-" in listing and "name=ccoin-proxy-" in listing
    assert removal[-3:] == ["-f", "0123456789ab", "fedcba987654"]  # the junk line was dropped, never executed
    assert all(isinstance(a, list) for a in calls)


def test_launcher_without_a_runner_does_nothing():
    sv.WslRunnerLauncher(sv.SupervisorSettings()).stop_job_containers()  # run=None: no error
