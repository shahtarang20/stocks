"""The heat watchdog and the orphan cleanup: the last line of defence when the agent itself is gone."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from computecoin_agent import cli, watchdog
from computecoin_agent.config import AgentConfig
from computecoin_agent.thermal import ThermalGuard

_REAL_KEEP_RUNNING = watchdog.keep_running  # the autouse fixture below replaces the module attribute

ID1, ID2, ID3, ID4 = ("a" * 12, "b" * 12, "c" * 64, "d" * 12)



@pytest.fixture(autouse=True)
def _no_background_watchdog_thread(monkeypatch):
    """_start_safeguards starts a thread that would, after 30 s, really start a watchdog process. Never in a test."""
    monkeypatch.setattr(watchdog, "keep_running", lambda *a, **k: None)


class FakeDocker:
    """Records every call; answers `ps` and `network ls` from a table."""

    def __init__(self, containers=(), networks=(), fail=False):
        self.containers = list(containers)  # [(id, "name1,name2")]
        self.networks = list(networks)
        self.calls = []
        self.fail = fail

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail:
            return None
        if argv[:2] == ["docker", "ps"]:
            return "\n".join(f"{i}\t{n}" for i, n in self.containers) + "\n"
        if argv[:3] == ["docker", "network", "ls"]:
            return "\n".join(self.networks) + "\n"
        return ""

    def removed(self):
        return [c[3:] for c in self.calls if c[:3] == ["docker", "rm", "-f"]]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _guard(temp, clock=None, lost=120.0):
    return ThermalGuard(85, 80, 95, sampler=lambda: [SimpleNamespace(temp_c=temp[0])] if temp[0] is not None else [],
                        sensor_lost_seconds=lost, clock=clock or Clock())


# ---- finding the customers' containers ----------------------------------------------------------------------------
def test_only_the_agents_own_containers_are_found_by_name_prefix():
    d = FakeDocker([
        (ID1, "ccoin-job-j1"), (ID2, "ccoin-sess-s1"), (ID3, "ccoin-proxy-j1"), (ID4, "cc-svc-123"),
        ("e" * 12, "my-database"),  # the owner's own container
        ("f" * 12, "not-ccoin-job-x"),  # the prefix must be at the START of the name
        ("9" * 12, "ccoin-job"),  # no trailing dash: not ours
    ])
    assert [n for _, n in watchdog.list_job_containers(d)] == ["ccoin-job-j1", "ccoin-sess-s1", "ccoin-proxy-j1", "cc-svc-123"]


def test_a_container_with_several_names_is_found_by_any_of_them():
    d = FakeDocker([(ID1, "linked,ccoin-job-j9")])
    assert watchdog.list_job_containers(d) == [(ID1, "ccoin-job-j9")]


def test_stopped_containers_are_only_listed_when_asked():
    d = FakeDocker([(ID1, "ccoin-job-j1")])
    watchdog.list_job_containers(d)
    watchdog.list_job_containers(d, include_stopped=True)
    assert "-a" not in d.calls[0] and "-a" in d.calls[1]


def test_garbage_and_failures_give_an_empty_list_not_a_crash():
    assert watchdog.list_job_containers(FakeDocker(fail=True)) == []
    bad = lambda argv: "not-an-id\tccoin-job-x\n\n\tccoin-job-y\n"  # noqa: E731
    assert watchdog.list_job_containers(bad) == []


def test_ids_are_checked_before_they_reach_the_command_line():
    d = FakeDocker()
    assert watchdog.remove_containers(["--privileged", "x y", ID1], d) is True
    assert d.removed() == [[ID1]]  # an odd value is dropped, never passed on as an option
    assert watchdog.remove_containers([], d) is True and len(d.calls) == 1  # nothing to do: no command


# ---- cleaning up after an earlier run -----------------------------------------------------------------------------
def test_orphans_and_their_networks_are_removed_but_the_owners_things_are_not():
    d = FakeDocker([(ID1, "ccoin-job-j1"), (ID2, "my-database")], networks=["bridge", "ccoin-net-j1", "ccoin-net-j2", "mynet"])
    r = watchdog.reap_orphans(d)
    assert (r.containers, r.networks) == (1, 2)
    assert d.removed() == [[ID1]]
    removed_nets = [c[3] for c in d.calls if c[:3] == ["docker", "network", "rm"]]
    assert removed_nets == ["ccoin-net-j1", "ccoin-net-j2"]


def test_cleaning_up_with_nothing_there_does_nothing():
    d = FakeDocker()
    assert watchdog.reap_orphans(d) == watchdog.ReapResult(0, 0)
    assert d.removed() == []


def test_cleanup_never_raises_even_if_docker_is_missing_or_explodes():
    assert watchdog.reap_orphans(FakeDocker(fail=True)) == watchdog.ReapResult(0, 0)

    def boom(argv):
        raise RuntimeError("docker exploded")

    assert watchdog.reap_orphans(boom) == watchdog.ReapResult(0, 0)


# ---- the watchdog -------------------------------------------------------------------------------------------------
def test_a_cool_gpu_is_left_alone():
    clock, t = Clock(), [70.0]
    d = FakeDocker([(ID1, "ccoin-job-j1")])
    dog = watchdog.HeatWatchdog(_guard(t, clock), docker=d, grace_seconds=30, clock=clock)
    for _ in range(10):
        clock.now += 10
        assert dog.step() == "ok"
    assert d.calls == [] and dog.kills == 0


def test_a_hot_gpu_is_given_the_agent_time_to_act_first():
    clock, t = Clock(), [96.0]
    d = FakeDocker([(ID1, "ccoin-job-j1")])
    dog = watchdog.HeatWatchdog(_guard(t, clock), docker=d, grace_seconds=30, clock=clock)
    assert dog.step() == "watching"
    clock.now += 29
    assert dog.step() == "watching"
    assert d.removed() == []  # the agent's own check (every 2 s) has the first 30 s


def test_a_gpu_still_hot_after_the_grace_period_loses_its_customer_containers_only():
    clock, t = Clock(), [96.0]
    d = FakeDocker([(ID1, "ccoin-job-j1"), (ID2, "my-database")])
    dog = watchdog.HeatWatchdog(_guard(t, clock), docker=d, grace_seconds=30, clock=clock)
    dog.step()
    clock.now += 30
    assert dog.step() == "killed"
    assert d.removed() == [[ID1]] and dog.kills == 1


def test_cooling_down_inside_the_grace_period_resets_it():
    clock, t = Clock(), [96.0]
    d = FakeDocker([(ID1, "ccoin-job-j1")])
    dog = watchdog.HeatWatchdog(_guard(t, clock), docker=d, grace_seconds=30, clock=clock)
    dog.step()
    clock.now += 20
    t[0] = 70.0
    assert dog.step() == "ok"
    t[0] = 96.0
    clock.now += 20
    assert dog.step() == "watching"  # a NEW period began, not 40 s of the old one
    assert d.removed() == []


def test_a_lost_sensor_is_handled_like_a_hot_gpu_but_only_after_the_sensor_limit_plus_the_grace():
    clock, t = Clock(), [60.0]
    guard = _guard(t, clock, lost=50)
    d = FakeDocker([(ID1, "ccoin-job-j1")])
    dog = watchdog.HeatWatchdog(guard, docker=d, grace_seconds=30, clock=clock)
    assert dog.step() == "ok"  # the sensor works
    t[0] = None
    assert dog.step() == "ok"  # just went quiet
    clock.now += 50
    assert dog.step() == "watching"  # lost; the agent has its 30 s
    clock.now += 30
    assert dog.step() == "killed"


def test_a_computer_that_never_had_a_sensor_is_never_touched():
    clock, t = Clock(), [None]
    d = FakeDocker([(ID1, "ccoin-job-j1")])
    dog = watchdog.HeatWatchdog(_guard(t, clock, lost=10), docker=d, grace_seconds=0, clock=clock)
    for _ in range(5):
        clock.now += 1000
        assert dog.step() == "ok"
    assert d.calls == []


def test_a_hot_gpu_with_nothing_running_removes_nothing_and_does_not_error():
    clock, t = Clock(), [99.0]
    d = FakeDocker()
    dog = watchdog.HeatWatchdog(_guard(t, clock), docker=d, grace_seconds=0, clock=clock)
    assert dog.step() == "killed" and d.removed() == [] and dog.kills == 0


def test_a_negative_grace_is_refused():
    with pytest.raises(ValueError):
        watchdog.HeatWatchdog(_guard([60.0]), grace_seconds=-1)


# ---- the loop and the alive file -----------------------------------------------------------------------------------
def test_the_loop_touches_its_file_survives_a_failing_step_and_stops_when_told(tmp_path):
    marker = tmp_path / "sub" / "watchdog.alive"
    stop = threading.Event()
    calls = []

    class Sensor:
        def __call__(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("driver exploded")  # read_max_temp swallows this
            if len(calls) >= 3:
                stop.set()
            return []

    guard = ThermalGuard(85, 80, 95, sampler=Sensor())
    t = threading.Thread(target=watchdog.run_forever, args=(guard, stop),
                         kwargs=dict(poll_seconds=0.01, grace_seconds=0, docker=FakeDocker(), path=marker))
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert marker.exists()
    assert len(calls) >= 3  # it kept going after the failure


def test_is_alive_means_a_recent_touch_and_nothing_else(tmp_path):
    marker = tmp_path / "w.alive"
    assert watchdog.is_alive(marker) is False  # no file
    marker.touch()
    mtime = os.path.getmtime(marker)
    assert watchdog.is_alive(marker, max_age=20, now=lambda: mtime + 5) is True
    assert watchdog.is_alive(marker, max_age=20, now=lambda: mtime + 25) is False  # stale
    assert watchdog.is_alive(marker, max_age=20, now=lambda: mtime - 5) is False  # a clock that went backwards is no proof


def test_a_running_watchdog_is_not_started_twice_and_a_missing_one_is_started():
    spawned = []
    assert watchdog.ensure_running(alive=lambda: True, spawn=lambda: spawned.append(1) or True) == "already_running"
    assert spawned == []
    assert watchdog.ensure_running(alive=lambda: False, spawn=lambda: spawned.append(1) or True) == "started"
    assert watchdog.ensure_running(alive=lambda: False, spawn=lambda: False) == "failed"


def test_the_unit_runs_the_watchdog_on_its_own():
    unit = watchdog.systemd_unit("/usr/local/bin/computecoin-agent")
    assert "ExecStart=/usr/local/bin/computecoin-agent watchdog" in unit
    assert "Restart=always" in unit and "[Install]" in unit


# ---- the agent's start-up ------------------------------------------------------------------------------------------
def _linux(monkeypatch):
    from computecoin_agent import platform as plat

    monkeypatch.setattr(plat, "get_platform", lambda: SimpleNamespace(name=plat.PLATFORM_LINUX))


def test_start_clears_leftovers_and_starts_the_watchdog_on_linux(monkeypatch):
    _linux(monkeypatch)
    monkeypatch.setattr(watchdog, "reap_orphans", lambda: watchdog.ReapResult(2, 1))
    monkeypatch.setattr(watchdog, "ensure_running", lambda: "started")
    said = []
    cli._start_safeguards(AgentConfig(), said.append)
    assert any("2 container(s) and 1 network(s)" in m for m in said)
    assert any("heat watchdog: started" in m for m in said)


def test_start_says_so_when_the_watchdog_cannot_be_started(monkeypatch):
    _linux(monkeypatch)
    monkeypatch.setattr(watchdog, "reap_orphans", lambda: watchdog.ReapResult())
    monkeypatch.setattr(watchdog, "ensure_running", lambda: "failed")
    said = []
    cli._start_safeguards(AgentConfig(), said.append)
    assert any("could not be started" in m for m in said)


def test_the_watchdog_can_be_switched_off_and_cleanup_still_runs(monkeypatch):
    _linux(monkeypatch)
    cleaned = []
    monkeypatch.setattr(watchdog, "reap_orphans", lambda: cleaned.append(1) or watchdog.ReapResult())
    monkeypatch.setattr(watchdog, "ensure_running", lambda: pytest.fail("must not start"))
    cli._start_safeguards(AgentConfig(heat_watchdog_enabled=False), lambda m: None)
    assert cleaned == [1]


def test_nothing_happens_on_windows_where_the_supervisor_does_this(monkeypatch):
    from computecoin_agent import platform as plat

    monkeypatch.setattr(plat, "get_platform", lambda: SimpleNamespace(name=plat.PLATFORM_WINDOWS))
    monkeypatch.setattr(watchdog, "reap_orphans", lambda: pytest.fail("must not run"))
    cli._start_safeguards(AgentConfig(), lambda m: None)


def test_a_failure_in_the_safeguards_never_stops_the_agent(monkeypatch):
    _linux(monkeypatch)

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(watchdog, "reap_orphans", boom)
    said = []
    cli._start_safeguards(AgentConfig(), said.append)  # must not raise
    assert any("skipped" in m for m in said)


def test_the_cli_command_exists_and_prints_a_unit():
    from typer.testing import CliRunner

    out = CliRunner().invoke(cli.app, ["watchdog", "--print-unit"])
    assert out.exit_code == 0
    assert "ExecStart=" in out.output and " watchdog" in out.output
    assert CliRunner().invoke(cli.app, ["watchdog", "--poll-seconds", "0"]).exit_code != 0  # too small a poll is refused


# ---- review fixes ---------------------------------------------------------------------------------------------------
def test_a_restart_does_not_kill_live_sessions_or_services_but_the_heat_watchdog_does():
    d = FakeDocker([(ID1, "ccoin-job-j1"), (ID2, "ccoin-sess-s1"), (ID3, "cc-svc-9"), (ID4, "ccoin-proxy-j1")])
    watchdog.reap_orphans(d)
    assert sorted(i for ids in d.removed() for i in ids) == sorted([ID1, ID4])  # sessions and services keep running
    clock, t = Clock(), [99.0]
    d2 = FakeDocker([(ID1, "ccoin-job-j1"), (ID2, "ccoin-sess-s1"), (ID3, "cc-svc-9")])
    dog = watchdog.HeatWatchdog(_guard(t, clock), docker=d2, grace_seconds=0, clock=clock)
    assert dog.step() == "killed" and len(d2.removed()[0]) == 3  # in an emergency everything goes


def test_the_alive_file_carries_the_poll_interval_so_a_slow_watchdog_is_not_called_dead(tmp_path):
    marker = tmp_path / "w.alive"
    stop = threading.Event()
    guard = ThermalGuard(85, 80, 95, sampler=lambda: stop.set() or [])
    watchdog.run_forever(guard, stop, poll_seconds=60, grace_seconds=0, docker=FakeDocker(), path=marker)
    assert marker.read_text() == "60"
    mtime = os.path.getmtime(marker)
    assert watchdog.is_alive(marker, now=lambda: mtime + 200) is True  # 200 s old, but it polls every 60 s: four polls = 240 s
    assert watchdog.is_alive(marker, now=lambda: mtime + 250) is False
    marker.write_text("garbage")
    assert watchdog.is_alive(marker, now=lambda: mtime + 10) is True  # unreadable interval: the 5 s default (20 s window)
    assert watchdog.is_alive(marker, now=lambda: mtime + 30) is False


def test_the_agent_starts_a_dead_watchdog_again_and_never_raises():
    stop = threading.Event()
    calls = []

    def ensure():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) >= 3:
            stop.set()
        return "started"

    t = threading.Thread(target=_REAL_KEEP_RUNNING, args=(stop,), kwargs=dict(every=0.01, ensure=ensure))
    t.start()
    t.join(5)
    assert not t.is_alive() and len(calls) >= 3  # it carried on after the failure


def test_start_keeps_the_watchdog_alive_with_a_background_thread(monkeypatch):
    _linux(monkeypatch)
    monkeypatch.setattr(watchdog, "reap_orphans", lambda: watchdog.ReapResult())
    monkeypatch.setattr(watchdog, "ensure_running", lambda: "started")
    started = []
    monkeypatch.setattr(watchdog, "keep_running", lambda *a, **k: started.append(1))
    cli._start_safeguards(AgentConfig(), lambda m: None)
    import time as _t

    deadline = _t.time() + 2
    while not started and _t.time() < deadline:
        _t.sleep(0.01)
    assert started == [1]


# ---- the unit must name an absolute command ------------------------------------------------------------------------------
def test_the_unit_command_is_always_absolute(tmp_path, monkeypatch):
    prog = tmp_path / "computecoin-agent"
    prog.write_text("#!/bin/sh\n")
    prog.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert watchdog.agent_command("computecoin-agent", "/py/python") == str(prog)  # a bare name is resolved
    assert watchdog.agent_command(str(prog), "/py/python") == str(prog)
    for odd in ("-c", "", "-m", "something-else", "nonexistent-program"):
        assert watchdog.agent_command(odd, "/py/python") == "/py/python -m computecoin_agent", odd


def test_the_printed_unit_never_has_a_relative_or_odd_command():
    from typer.testing import CliRunner

    out = CliRunner().invoke(cli.app, ["watchdog", "--print-unit"]).output
    line = next(line for line in out.splitlines() if line.startswith("ExecStart="))
    assert line.startswith("ExecStart=/") and line.endswith(" watchdog"), line


def test_the_agent_can_be_run_as_a_module():
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-m", "computecoin_agent", "--help"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "watchdog" in done.stdout


def test_the_background_watchdog_is_started_the_same_way(monkeypatch):
    seen = []
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    assert watchdog.spawn_detached() is True
    argv, kw = seen[0]
    assert argv[1:] == ["-m", "computecoin_agent", "watchdog"] and kw["start_new_session"] is True
