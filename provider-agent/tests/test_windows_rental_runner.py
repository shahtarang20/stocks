"""Container rental on a Windows host (EXPERIMENTAL): the runner inside the managed Linux system serves rentals and nothing else.

Pins: the capability flag needs BOTH host switches on Windows; the supervisor starts the runner with --serve-rentals only then; a
rentals-only runner refuses any session that is not a rental; rentals count as running jobs for the supervisor and obey its
start, drain and stop; Linux behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from computecoin_agent import capabilities as caps
from computecoin_agent import cli
from computecoin_agent import session_agent as sa
from computecoin_agent.platform import PLATFORM_WINDOWS
from computecoin_agent.platform.fake import make_fake_platform
from computecoin_agent.windows import supervisor as sv
from test_container_rental_agent import FakeApi, Runner, SID, _beats, _claim, agent_for, run_serve


linux_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="a rental's workspace is made inside the Linux system, with POSIX file handling")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(sa, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(sa, "HEARTBEAT_RETRY_SECONDS", 0.01)
    real = asyncio.sleep

    async def quick(delay, *a, **k):
        return await real(min(delay, 0.02), *a, **k)

    monkeypatch.setattr(sa.asyncio, "sleep", quick)


class Activity:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def job_started(self, sid):
        self.events.append(("started", sid))
        if self.fail:
            raise RuntimeError("counter broke")

    def job_finished(self, sid, status):
        self.events.append(("finished", sid, status))
        if self.fail:
            raise RuntimeError("counter broke")


# ---- the capability flag ---------------------------------------------------------------------------------------------------
def _cfg(**kw):
    base = dict(gpu_power_limit_w=None, gpu_power_limit_percent=None, egress_proxy_image=None, cpu_only_kits_enabled=False,
                multinode_network=None, multinode_network_firewalled=False, allow_container_rental=False, windows_rental_enabled=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _flags(config, os_name=PLATFORM_WINDOWS):
    rep = SimpleNamespace(gpus=[], isolation_tiers=["container", "gvisor", "microvm", "sealed", "confidential"], contract_summary=lambda: {})
    return caps.compute_capabilities(rep, config=config, plat=make_fake_platform(os_name), isolation_runtimes_fn=lambda: ["gvisor"], vfio_fn=lambda: True)


def test_a_windows_host_needs_both_switches_for_the_rental_flag():
    assert "container_rental" not in _flags(_cfg())
    assert "container_rental" not in _flags(_cfg(allow_container_rental=True))  # the experimental switch is off
    assert "container_rental" not in _flags(_cfg(windows_rental_enabled=True))  # the host never opted in
    assert "container_rental" in _flags(_cfg(allow_container_rental=True, windows_rental_enabled=True))


@pytest.mark.parametrize("lookalike", ["true", 1, "yes", [], {}, None])
def test_only_a_real_true_counts_for_the_windows_switch(lookalike):
    assert "container_rental" not in _flags(_cfg(allow_container_rental=True, windows_rental_enabled=lookalike))


def test_the_windows_switch_frees_only_the_rental_flag():
    out = _flags(_cfg(allow_container_rental=True, windows_rental_enabled=True))
    assert "container_rental" in out
    assert not (set(out) & (caps.WINDOWS_FORBIDDEN_FLAGS - {"container_rental"}))  # gvisor, microvm, private tiers, vfio, multi-node stay out


def test_the_windows_switch_changes_nothing_on_linux():
    assert "container_rental" in _flags(_cfg(allow_container_rental=True), "linux")
    assert "container_rental" in _flags(_cfg(allow_container_rental=True, windows_rental_enabled=True), "linux")
    assert "container_rental" not in _flags(_cfg(windows_rental_enabled=True), "linux")


def test_no_container_engine_means_no_flag_on_windows_either():
    rep = SimpleNamespace(gpus=[], isolation_tiers=["container"], contract_summary=lambda: {})
    out = caps.compute_capabilities(rep, config=_cfg(allow_container_rental=True, windows_rental_enabled=True),
                                    plat=make_fake_platform(PLATFORM_WINDOWS, docker_available=False),
                                    isolation_runtimes_fn=lambda: [], vfio_fn=lambda: None)
    assert "container_rental" not in out


# ---- the supervisor ---------------------------------------------------------------------------------------------------------
def test_the_supervisor_asks_for_rentals_only_when_both_switches_are_on():
    assert sv.serve_rentals_enabled(_cfg(allow_container_rental=True, windows_rental_enabled=True)) is True
    for one in (dict(allow_container_rental=True), dict(windows_rental_enabled=True), {}):
        assert sv.serve_rentals_enabled(_cfg(**one)) is False
    for lookalike in ("true", 1, None):
        assert sv.serve_rentals_enabled(_cfg(allow_container_rental=lookalike, windows_rental_enabled=lookalike)) is False


def test_the_runner_is_started_with_serve_rentals_only_when_asked():
    plain = sv.WslRunnerLauncher(sv.SupervisorSettings()).runner_argv()
    assert "--serve-rentals" not in plain
    on = sv.WslRunnerLauncher(sv.SupervisorSettings(serve_rentals=True)).runner_argv()
    assert on[-1] == "--serve-rentals" and on[:len(plain)] == plain  # everything else is exactly as before
    assert not any("sh" == a.rsplit("/", 1)[-1] for a in on)  # still no shell


def test_the_supervisor_setting_defaults_off_and_unknown_settings_are_still_refused():
    assert sv.SupervisorSettings().serve_rentals is False
    assert sv.SupervisorSettings.from_dict({"serve_rentals": True}).serve_rentals is True
    with pytest.raises(ValueError):
        sv.SupervisorSettings.from_dict({"serve_rental": True})


# ---- the runner: rentals only ----------------------------------------------------------------------------------------------
@linux_only
def test_a_rentals_only_runner_serves_a_rental_and_reports_it_to_the_supervisor(tmp_path):
    activity = Activity()
    api = FakeApi(**{"/heartbeat": _beats(3)})
    ag = agent_for(tmp_path, api, Runner(), rentals_only=True, activity=activity)
    assert run_serve(ag, _claim()) == "stop"
    assert activity.events == [("started", SID), ("finished", SID, "succeeded")]


@linux_only
def test_a_rentals_only_runner_refuses_a_session_that_is_not_a_rental(tmp_path):
    activity = Activity()
    runner = Runner()
    api = FakeApi(**{"/heartbeat": _beats(3)})
    ag = agent_for(tmp_path, api, runner, rentals_only=True, activity=activity)
    with pytest.raises(sa.SessionAgentError, match="only takes container rentals"):
        run_serve(ag, _claim(rental=False))
    assert not runner.ran("docker", "run")  # nothing was started
    assert activity.events == []  # and it never counted as a running job
    assert api.body("/ended") == {"failed": True}  # the control plane is told it failed, so it is not left hanging
    assert f"/{SID}/started" not in api.paths()


@linux_only
def test_an_ordinary_runner_is_unchanged(tmp_path):
    api = FakeApi(**{"/heartbeat": _beats(3)})
    ag = agent_for(tmp_path, api, Runner())
    assert ag._rentals_only is False and ag._activity is None
    assert run_serve(ag, _claim()) == "stop"


@pytest.mark.parametrize("look_alike", ["true", 1, None])
def test_only_a_real_true_makes_a_runner_rentals_only(tmp_path, look_alike):
    ag = agent_for(tmp_path, FakeApi(), Runner(), rentals_only=look_alike)
    assert ag._rentals_only is False


@linux_only
def test_a_counter_that_breaks_never_breaks_a_rental(tmp_path):
    activity = Activity(fail=True)
    api = FakeApi(**{"/heartbeat": _beats(3)})
    ag = agent_for(tmp_path, api, Runner(), rentals_only=True, activity=activity)
    assert run_serve(ag, _claim()) == "stop"
    assert [e[0] for e in activity.events] == ["started", "finished"]
    assert api.body("/ended") == {"failed": False}


@linux_only
def test_a_rental_that_fails_to_start_is_reported_as_finished_only_if_it_started(tmp_path):
    activity = Activity()
    runner = Runner(run_rc=1)  # docker run fails
    api = FakeApi(**{"/heartbeat": _beats(3)})
    ag = agent_for(tmp_path, api, runner, rentals_only=True, activity=activity)
    with pytest.raises(sa.SessionAgentError):
        run_serve(ag, _claim())
    assert activity.events == []  # it never started, so the supervisor never saw it


# ---- the background service: the supervisor's gate -------------------------------------------------------------------------
def _start(extra_gate=None, **kw):
    from computecoin_agent.platform import use_platform

    seen = {}

    def loop(api, executor, *, agent):
        seen["agent"] = agent

    cfg_ = SimpleNamespace(serve_sessions=True, api_url="https://api.example", machine_id="m", machine_secret="s", allow_container_rental=True,
                           rental_root=None, work_dir=None, rental_max_workspace_gb=50, rental_max_memory_gb=0, rental_max_cpus=0)
    with use_platform(make_fake_platform()):
        t = sa.start_background_sessions(cfg_, engine_available=lambda: True, run_loop=loop, api=object(), executor=sa.DockerExecutor(
            docker_available=lambda: True, nvidia_available=lambda: True), extra_gate=extra_gate, **kw)
    t.join(5)
    return seen["agent"]


def test_the_supervisors_gate_decides_whether_the_runner_may_claim():
    allowed = {"v": True}
    ag = _start(extra_gate=lambda: allowed["v"])
    assert ag.claim_gate() is True
    allowed["v"] = False
    assert ag.claim_gate() is False  # draining, stopped, or not online: no new rental is taken


def test_no_extra_gate_means_the_old_behaviour():
    assert _start().claim_gate() is True


def test_rentals_only_and_the_activity_counter_reach_the_session_agent():
    activity = Activity()
    ag = _start(rentals_only=True, activity=activity)
    assert ag._rentals_only is True and ag._activity is activity


# ---- the work command's flag -----------------------------------------------------------------------------------------------
class _Control:
    def __init__(self, ok=True):
        self.ok = ok

    def may_claim(self):
        return self.ok


class _Cfg(SimpleNamespace):
    def model_copy(self, update):
        return _Cfg(**{**self.__dict__, **update})


def test_the_work_flag_turns_rental_on_for_this_process_and_wires_the_supervisor(monkeypatch):
    got = {}
    messages = []

    def fake_start(config, **kw):
        got.update(kw, config=config)
        return object()

    monkeypatch.setattr(sa, "start_background_sessions", fake_start)
    control = _Control()
    config = _Cfg(allow_container_rental=False, other="x")
    cli._serve_rentals_from_runner(config, "guard", control, messages.append)
    assert got["config"].allow_container_rental is True and got["config"].other == "x"
    assert config.allow_container_rental is False  # the caller's config is not changed
    assert got["rentals_only"] is True and got["activity"] is control and got["thermal_guard"] == "guard"
    assert got["extra_gate"]() is True
    control.ok = False
    assert got["extra_gate"]() is False
    assert messages == ["container rental: serving rentals only"]


def test_the_work_flag_says_so_when_there_is_no_usable_container_engine(monkeypatch):
    monkeypatch.setattr(sa, "start_background_sessions", lambda config, **kw: None)
    messages = []
    cli._serve_rentals_from_runner(_Cfg(allow_container_rental=False), None, None, messages.append)
    assert messages == ["container rental: not started (no usable container engine)"]


def test_the_work_command_has_the_flag_and_it_is_off_by_default():
    from typer.testing import CliRunner

    import re

    # CI forces colour and a narrow width, which splits "--serve-rentals" with style codes and line breaks: ask for plain text, and strip any that remain.
    out = CliRunner().invoke(cli.app, ["work", "--help"], env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}).output
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert "--serve-rentals" in out and "EXPERIMENTAL" in out.upper()


# ---- leftovers after a crash or a restart ----------------------------------------------------------------------------------
UUID_A = "11111111-2222-4333-8444-555555555555"
UUID_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"


def _ps_runner(names, *, ps_rc=0, rm_rc=0):
    calls = []

    def runner(argv):
        calls.append(list(argv))
        if argv[:3] == ["docker", "ps", "-a"]:
            return SimpleNamespace(returncode=ps_rc, stdout="\n".join(names) + "\n", stderr="")
        if argv[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=rm_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.calls = calls
    return runner


def test_a_restart_removes_session_containers_a_dead_agent_left_behind(tmp_path):
    runner = _ps_runner([f"ccoin-sess-{UUID_A}", f"ccoin-sess-{UUID_B}"])
    ag = agent_for(tmp_path, FakeApi(), runner)
    assert ag.remove_orphan_containers() == 2
    removed = [c[3] for c in runner.calls if c[:3] == ["docker", "rm", "-f"]]
    assert removed == [f"ccoin-sess-{UUID_A}", f"ccoin-sess-{UUID_B}"]
    listing = next(c for c in runner.calls if c[:3] == ["docker", "ps", "-a"])
    assert "name=ccoin-sess-" in listing


def test_only_exact_session_container_names_are_ever_removed(tmp_path):
    runner = _ps_runner([f"ccoin-sess-{UUID_A}", "pgas-db", "my-ccoin-sess-x", f"x-ccoin-sess-{UUID_B}", "ccoin-job-abc123", "ccoin-sess-", "ccoin-sess-not an id",
                         "ccoin-sess-$(id)-0000000", f"ccoin-sess-{UUID_B}; rm -rf /"])
    ag = agent_for(tmp_path, FakeApi(), runner)
    assert ag.remove_orphan_containers() == 1
    assert [c[3] for c in runner.calls if c[:3] == ["docker", "rm", "-f"]] == [f"ccoin-sess-{UUID_A}"]


def test_nothing_is_removed_when_docker_cannot_be_asked_and_a_failed_removal_is_not_counted(tmp_path):
    runner = _ps_runner([f"ccoin-sess-{UUID_A}"], ps_rc=1)
    assert agent_for(tmp_path, FakeApi(), runner).remove_orphan_containers() == 0
    assert not [c for c in runner.calls if c[:3] == ["docker", "rm", "-f"]]
    runner = _ps_runner([f"ccoin-sess-{UUID_A}"], rm_rc=1)
    assert agent_for(tmp_path, FakeApi(), runner).remove_orphan_containers() == 0


def test_a_session_this_process_is_running_is_never_removed(tmp_path):
    runner = _ps_runner([f"ccoin-sess-{UUID_A}", f"ccoin-sess-{UUID_B}"])
    ag = agent_for(tmp_path, FakeApi(), runner)
    ag._runs[UUID_A] = object()  # a rental this agent is serving right now
    assert ag.remove_orphan_containers() == 1
    assert [c[3] for c in runner.calls if c[:3] == ["docker", "rm", "-f"]] == [f"ccoin-sess-{UUID_B}"]


def test_the_session_loop_clears_leftover_containers_before_the_folders_and_before_any_claim(tmp_path):
    order = []

    class Agent:
        claim_gate = None

        def remove_orphan_containers(self):
            order.append("containers")

        def cleanup_stale(self):
            order.append("folders")

        def claim(self, key):
            order.append("claim")
            return None

    sa.run_forever(object(), object(), once=True, sleeper=lambda s: None, agent=Agent())
    assert order[:2] == ["containers", "folders"] and "claim" in order[2:]


def test_a_failing_container_cleanup_never_stops_the_loop(tmp_path):
    order = []

    class Agent:
        claim_gate = None

        def remove_orphan_containers(self):
            raise RuntimeError("docker is gone")

        def cleanup_stale(self):
            order.append("folders")

        def claim(self, key):
            order.append("claim")
            return None

    sa.run_forever(object(), object(), once=True, sleeper=lambda s: None, agent=Agent())
    assert order == ["folders", "claim"]


@linux_only
def test_a_running_rental_counts_as_a_running_job_for_the_windows_service_and_blocks_a_second_claim(tmp_path):
    """The supervisor's keep-awake, update and drain logic all read this counter, so a rental must be in it while it runs."""
    from computecoin_agent import control_channel as cc

    control = cc.RunnerControl(agent_version="test")
    control.handle(cc.ControlMessage("start", {}))
    assert control.claim_block_reason() is None  # started, online, idle: may claim
    seen = []
    inner = _beats(4)

    def beat(body):
        seen.append(control.claim_block_reason())
        return inner(body)

    api = FakeApi(**{"/heartbeat": beat})
    ag = agent_for(tmp_path, api, Runner(), rentals_only=True, activity=control)
    assert run_serve(ag, _claim()) == "stop"
    assert "at_capacity" in seen and set(seen) == {"at_capacity"}  # during the rental: busy, no second claim
    assert control.claim_block_reason() is None  # after it: free again
    assert control._completed == 1 and control._failed == 0  # and it was reported as a success


@linux_only
def test_a_failed_rental_is_reported_to_the_windows_service_as_failed(tmp_path):
    from computecoin_agent import control_channel as cc

    control = cc.RunnerControl(agent_version="test")
    control.handle(cc.ControlMessage("start", {}))
    api = FakeApi(**{"/heartbeat": _beats(4)})
    ag = agent_for(tmp_path, api, Runner(), rentals_only=True, activity=control)
    ag._aborts[SID] = "thermal"  # the monitor ended it because the card got too hot
    assert run_serve(ag, _claim()) in ("stop", "ended")
    assert control._failed == 1 and control._completed == 0 and control.claim_block_reason() is None

