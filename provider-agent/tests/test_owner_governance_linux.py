"""Linux: step aside when the owner is busy. Probes, decisions over time, the pause switch, and the agent wiring."""

from __future__ import annotations

import datetime as dt
import json
import types

import httpx
import pytest
from typer.testing import CliRunner

from computecoin_agent import cli, interrupts
from computecoin_agent.heartbeat import heartbeat_loop
from computecoin_agent.jobrunner import poll_loop
from computecoin_agent.owner_activity import (
    LINUX_IGNORED_PROCESSES,
    LinuxActivityProbe,
    NvidiaGpuUsers,
    parse_logind_idle,
    parse_pmon,
)
from computecoin_agent.owner_governance import LinuxGovernance, linux_policy, owner_stop_path
from computecoin_agent.platform import ActivitySnapshot, PowerSnapshot
from computecoin_agent.platform.linux import LinuxPower


@pytest.fixture(autouse=True)
def _clean_registry():
    interrupts.REGISTRY.__init__()
    yield
    interrupts.REGISTRY.__init__()


# ===== nvidia-smi: other programs on the GPU ===========================================================================
PMON = """\
# gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command
# Idx           #    C/G      %      %      %      %      %      %    name
    0       1423     G      3      1      0      0      -      -    Xorg
    0       2210     G      1      0      0      0      -      -    gnome-shell
    0       9001   C+G     67     22      -      -      -      -    /home/me/games/Cyberpunk2077
    0       7777     C     80     40      -      -      -      -    python3
"""


def test_pmon_parsing_reads_pid_type_and_command():
    procs = parse_pmon(PMON)
    assert [(p.pid, p.kind, p.command) for p in procs] == [
        (1423, "G", "Xorg"), (2210, "G", "gnome-shell"), (9001, "C+G", "/home/me/games/Cyberpunk2077"), (7777, "C", "python3"),
    ]


def test_pmon_parsing_skips_junk_and_empty_output():
    assert parse_pmon("") == []
    assert parse_pmon("# only a header\n") == []
    assert parse_pmon("No devices were found\nrandom words here now\n") == []


def _cgroup_tree(tmp_path, mapping):
    for pid, text in mapping.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cgroup").write_text(text)
    return str(tmp_path)


def test_other_processes_ignores_desktop_plumbing_and_job_containers_but_keeps_the_owners_game(tmp_path):
    proc = _cgroup_tree(tmp_path, {
        1423: "0::/user.slice/user-1000.slice/session-2.scope\n",
        2210: "0::/user.slice/user-1000.slice/session-2.scope\n",
        9001: "0::/user.slice/user-1000.slice/user@1000.service/app.slice/steam.scope\n",
        7777: "0::/system.slice/docker-0123456789abcdef.scope\n",  # a job's container
    })
    users = NvidiaGpuUsers(lambda argv: PMON, proc_root=proc)
    assert users.other_processes() == ["cyberpunk2077"]  # basename, lower case
    assert users.foreground_gpu_app() == "cyberpunk2077"


def test_when_nvidia_smi_cannot_be_read_the_answer_is_unknown_not_free(tmp_path):
    users = NvidiaGpuUsers(lambda argv: None, proc_root=str(tmp_path))
    assert users.other_processes() is None
    assert users.foreground_gpu_app() is None
    assert users.free_vram_mb() is None


def test_nobody_else_on_the_gpu_is_an_empty_list_not_none(tmp_path):
    users = NvidiaGpuUsers(lambda argv: "# gpu pid type sm mem command\n# Idx # C/G % % name\n", proc_root=str(tmp_path))
    assert users.other_processes() == []


def test_a_process_that_cannot_be_inspected_counts_as_the_owner_not_as_a_job(tmp_path):
    # no /proc/<pid>/cgroup at all (the process vanished, or hidepid): be careful, assume it is the owner's
    users = NvidiaGpuUsers(lambda argv: "    0   4242     G     5     1     -     -     -     -    blender\n", proc_root=str(tmp_path))
    assert users.other_processes() == ["blender"]


def test_the_ignore_list_can_be_extended_by_the_owner(tmp_path):
    proc = _cgroup_tree(tmp_path, {9001: "0::/user.slice\n"})
    text = "    0   9001   C+G    10    10     -     -     -     -    obs\n"
    assert NvidiaGpuUsers(lambda a: text, proc_root=proc).other_processes() == ["obs"]
    assert NvidiaGpuUsers(lambda a: text, proc_root=proc, ignored=LINUX_IGNORED_PROCESSES | {"obs"}).other_processes() == []


def test_free_vram_is_the_most_free_on_any_card_and_odd_output_is_unknown():
    assert NvidiaGpuUsers(lambda a: "812\n20480\n").free_vram_mb() == 20480
    assert NvidiaGpuUsers(lambda a: "[N/A]\n").free_vram_mb() is None
    assert NvidiaGpuUsers(lambda a: "").free_vram_mb() is None


# ===== logind: is the owner at the keyboard =============================================================================
def test_logind_idle_hint_no_means_the_owner_is_there():
    snap = parse_logind_idle("IdleHint=no\nIdleSinceHint=0\n", now=1_700_000_000.0)
    assert snap.owner_active is True and snap.idle_seconds == 0.0


def test_logind_idle_hint_yes_gives_how_long_it_has_been_idle():
    since_us = int((1_700_000_000.0 - 900) * 1_000_000)
    snap = parse_logind_idle(f"IdleHint=yes\nIdleSinceHint={since_us}\n", now=1_700_000_000.0)
    assert snap.owner_active is False and snap.idle_seconds == pytest.approx(900.0)


def test_logind_nonsense_is_unknown_and_never_a_guess():
    for text in ("", "garbage", "IdleHint=maybe\n", "IdleHint=yes\nIdleSinceHint=abc\n", "IdleHint=yes\nIdleSinceHint=0\n"):
        assert parse_logind_idle(text, now=1.0) == ActivitySnapshot()


def test_no_loginctl_means_unknown():
    assert LinuxActivityProbe(lambda argv: None).snapshot() == ActivitySnapshot()


def test_the_probe_asks_logind_about_the_current_session():
    seen = []
    LinuxActivityProbe(lambda argv: seen.append(argv) or "IdleHint=no\n", session_id="c2").snapshot()
    assert seen[0][:3] == ["loginctl", "show-session", "c2"]


# ===== /sys/class/power_supply ==========================================================================================
def _supply(tmp_path, name, **files):
    d = tmp_path / name
    d.mkdir()
    for k, v in files.items():
        (d / k).write_text(str(v))


def test_laptop_on_battery_is_on_battery(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", status="Discharging")
    _supply(tmp_path, "AC", type="Mains", online=0)
    assert LinuxPower(str(tmp_path)).snapshot() == PowerSnapshot(on_battery=True)


def test_laptop_on_the_charger_is_not_on_battery(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", status="Charging")
    _supply(tmp_path, "AC", type="Mains", online=1)
    assert LinuxPower(str(tmp_path)).snapshot() == PowerSnapshot(on_battery=False)


def test_a_desktop_or_server_with_no_battery_is_not_on_battery(tmp_path):
    _supply(tmp_path, "AC", type="Mains", online=1)
    assert LinuxPower(str(tmp_path)).snapshot() == PowerSnapshot(on_battery=False)
    assert LinuxPower(str(tmp_path / "empty-but-existing")).snapshot() == PowerSnapshot()  # directory missing: unknown


def test_a_mouse_or_headset_battery_is_not_the_computers_battery(tmp_path):
    _supply(tmp_path, "hid-mouse", type="Battery", scope="Device", status="Discharging")
    assert LinuxPower(str(tmp_path)).snapshot() == PowerSnapshot(on_battery=False)


def test_a_battery_with_no_charger_seen_and_not_discharging_is_unknown_not_a_guess(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", status="Unknown")
    assert LinuxPower(str(tmp_path)).snapshot() == PowerSnapshot()


# ===== the decisions ====================================================================================================
class _Activity:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self):
        return self.snap


class _Gpu:
    def __init__(self, app=None, vram=None):
        self.app, self.vram = app, vram

    def foreground_gpu_app(self):
        return self.app

    def free_vram_mb(self):
        return self.vram


class _Clock:
    def __init__(self):
        self.t = 0.0

    def advance(self, s):
        self.t += s

    def mono(self):
        return self.t

    def now(self):
        return dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=self.t)


QUIET = ActivitySnapshot(owner_active=False, idle_seconds=3600.0)
AT_KEYBOARD = ActivitySnapshot(owner_active=True, idle_seconds=0.0)
ON_MAINS = PowerSnapshot(on_battery=False)


def _gov(config=None, *, activity=QUIET, power=ON_MAINS, gpu=None, stop_file=None, running=0, interrupted=None):
    clock = _Clock()
    interrupted = interrupted if interrupted is not None else []
    gov = LinuxGovernance(
        linux_policy(config or types.SimpleNamespace()),
        activity=_Activity(activity), power=_Activity(power), owner_gpu=gpu or _Gpu(),
        stop_file=stop_file, now_fn=clock.now, clock=clock.mono, min_interval_seconds=0.0,
        interrupt=interrupted.append, running=lambda: running,
    )
    return gov, clock, interrupted


def _settle(gov, clock, seconds=61):
    gov.evaluate()  # the first quiet sample starts the clear period
    clock.advance(seconds)
    return gov.evaluate()


def test_a_quiet_machine_comes_online_only_after_a_minute_of_calm():
    gov, clock, _ = _gov()
    first = gov.evaluate()
    assert first.state.value == "yielding" and first.code == "settling"  # starts closed (fail closed)
    assert gov.claim_block_reason() == "settling"
    assert _settle(gov, clock).state.value == "online"
    assert gov.claim_block_reason() is None and gov.availability() == "online"


def test_the_owner_at_the_keyboard_stops_new_jobs_at_once_and_the_heartbeat_says_throttled():
    gov, clock, _ = _gov(activity=AT_KEYBOARD)
    clock.advance(1000)
    d = gov.evaluate()
    assert d.state.value == "yielding"
    assert gov.claim_block_reason() in ("recent_input", "owner_active")
    assert gov.availability() == "throttled"
    assert gov.health_extras() == {"availability_detail": {"state": "yielding", "code": d.code}}


def test_on_battery_blocks_new_jobs_unless_the_owner_allowed_it():
    gov, clock, _ = _gov(power=PowerSnapshot(on_battery=True))
    assert _settle(gov, clock).code == "on_battery"
    allowed, c2, _ = _gov(types.SimpleNamespace(governance_allow_on_battery=True), power=PowerSnapshot(on_battery=True))
    assert _settle(allowed, c2).state.value == "online"


def test_another_program_using_the_gpu_blocks_new_jobs():
    gov, clock, _ = _gov(gpu=_Gpu(app="cyberpunk2077"))
    d = _settle(gov, clock)
    assert d.state.value == "yielding" and d.code == "foreground_gpu_app" and "cyberpunk2077" in d.reason


def test_short_gpu_memory_blocks_new_jobs_but_a_running_job_does_not_block_itself():
    gov, clock, _ = _gov(gpu=_Gpu(vram=300))
    assert _settle(gov, clock).code == "vram_reserve"
    busy, c2, _ = _gov(gpu=_Gpu(vram=300), running=1)  # our own job is using the memory
    assert _settle(busy, c2).state.value == "online"


def test_linux_cannot_see_fullscreen_or_reboots_so_those_unknowns_never_block():
    policy = linux_policy(types.SimpleNamespace())
    assert set(policy.unknown_blocks) == {"idle", "battery"}
    assert policy.preempt_on_fullscreen is False


def test_an_unreadable_idle_state_blocks_on_a_desktop_but_not_on_a_headless_server():
    unknown = ActivitySnapshot()
    gov, clock, _ = _gov(activity=unknown)
    assert _settle(gov, clock).code == "unknown_idle"  # cannot tell whether the owner is there: be careful
    server, c2, _ = _gov(types.SimpleNamespace(governance_headless=True), activity=unknown)
    assert _settle(server, c2).state.value == "online"


def test_a_running_job_is_never_killed_just_because_the_owner_came_back():
    gov, clock, interrupted = _gov(activity=AT_KEYBOARD, running=1)
    clock.advance(1000)
    d = gov.evaluate()
    assert d.state.value == "draining"  # it finishes; nothing new is taken
    assert d.stop_job is False and interrupted == []


def test_a_probe_that_crashes_blocks_new_jobs_instead_of_crashing_the_agent():
    class Boom:
        def snapshot(self):
            raise RuntimeError("loginctl exploded")

    gov, clock, _ = _gov()
    gov._activity = Boom()
    d = _settle(gov, clock)
    assert d.state.value == "yielding" and d.code == "probe_error"


def test_evaluations_are_cached_so_two_callers_do_not_hammer_the_system_tools():
    calls = []

    class CountingGpu(_Gpu):
        def foreground_gpu_app(self):
            calls.append(1)

    clock = _Clock()
    gov = LinuxGovernance(
        linux_policy(types.SimpleNamespace()), activity=_Activity(QUIET), power=_Activity(ON_MAINS), owner_gpu=CountingGpu(),
        now_fn=clock.now, clock=clock.mono, min_interval_seconds=2.0, running=lambda: 0, interrupt=lambda r: None,
    )
    gov.evaluate(); gov.evaluate(); gov.availability(); gov.claim_block_reason()
    assert len(calls) == 1
    clock.advance(3)
    gov.evaluate()
    assert len(calls) == 2


# ===== the owner's pause switch ========================================================================================
def test_pausing_stops_the_running_job_cleanly_and_blocks_new_ones_until_resume(tmp_path):
    stop = tmp_path / "owner-stop"
    gov, clock, interrupted = _gov(stop_file=stop, running=1)
    _settle(gov, clock)  # online, a job is running
    assert interrupted == []

    stop.write_text("paused\n")
    clock.advance(1)
    d = gov.evaluate()
    assert d.code == "owner_stop_now" and d.stop_job is True
    assert interrupted and interrupted[-1].startswith("owner:owner_stop_now")  # the running job is told to stop
    assert gov.claim_block_reason() == "owner_stop_now"

    stop.unlink()  # `computecoin-agent resume`
    clock.advance(1)
    gov.evaluate()
    assert gov.engine.stopped_by_owner is False
    # not instantly back: the normal calm period still applies
    assert gov.claim_block_reason() == "settling"
    clock.advance(61)
    assert gov.claim_block_reason() is None


def test_pausing_with_no_job_running_just_blocks_claims(tmp_path):
    stop = tmp_path / "owner-stop"
    gov, clock, interrupted = _gov(stop_file=stop, running=0)
    _settle(gov, clock)
    stop.write_text("paused\n")
    clock.advance(1)
    assert gov.claim_block_reason() == "owner_stop_now"
    assert interrupted == []


def test_the_pause_marker_lives_in_the_agents_config_folder(tmp_path):
    assert owner_stop_path(tmp_path) == tmp_path / "owner-stop"


# ===== the agent wiring ================================================================================================
def test_poll_loop_takes_no_job_while_the_owner_gate_says_no():
    class Gate:
        def claim_block_reason(self):
            return "recent_input"

    out = poll_loop("http://api.invalid", "m1", "secret", iterations=2, sleeper=lambda s: None, owner_gate=Gate())
    assert out == [{"claimed": False, "held": "recent_input"}] * 2  # never even asked the API for a job


def test_poll_loop_fails_closed_when_the_gate_itself_breaks():
    class Gate:
        def claim_block_reason(self):
            raise RuntimeError("boom")

    out = poll_loop("http://api.invalid", "m1", "secret", iterations=1, sleeper=lambda s: None, owner_gate=Gate())
    assert out == [{"claimed": False, "held": "owner_check_failed"}]


def test_the_heartbeat_carries_the_owner_state_and_the_ordinary_health_fields():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "directives": {}})

    heartbeat_loop(
        "http://api.invalid", "m1", "msk_" + "x" * 40, report=types.SimpleNamespace(
            contract_summary=lambda: {"gpu_count": 1, "vram_mb": 8192}, gpus=[], isolation_tiers=[],
        ),
        iterations=1, sleeper=lambda s: None, transport=httpx.MockTransport(handler),
        availability_fn=lambda: "throttled",
        health_extras=lambda: {"availability_detail": {"state": "yielding", "code": "recent_input"}},
    )
    body = seen[0]
    assert body["availability"] == "throttled"
    assert body["health"]["availability_detail"] == {"state": "yielding", "code": "recent_input"}
    assert "uptime_s" in body["health"]  # the normal health block is still there


def test_a_failing_health_extras_provider_never_stops_the_heartbeat():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "directives": {}})

    def broken():
        raise RuntimeError("nope")

    heartbeat_loop(
        "http://api.invalid", "m1", "msk_" + "x" * 40, report=types.SimpleNamespace(
            contract_summary=lambda: {"gpu_count": 0, "vram_mb": 0}, gpus=[], isolation_tiers=[],
        ),
        iterations=1, sleeper=lambda s: None, transport=httpx.MockTransport(handler), health_extras=broken,
    )
    assert len(seen) == 1 and "availability_detail" not in seen[0]["health"]


def test_governance_is_off_by_default_so_servers_keep_working_exactly_as_before():
    assert cli._owner_governance(types.SimpleNamespace(governance_enabled=None)) is None
    assert cli._owner_governance(types.SimpleNamespace(governance_enabled=False)) is None
    assert cli._owner_governance(types.SimpleNamespace()) is None


def test_governance_turns_on_when_the_owner_asks_for_it_on_linux(monkeypatch):
    from computecoin_agent import platform as plat

    monkeypatch.setattr(plat, "get_platform", lambda: types.SimpleNamespace(name="linux"))
    gov = cli._owner_governance(types.SimpleNamespace(governance_enabled=True))
    assert isinstance(gov, LinuxGovernance)


def test_the_windows_supervisor_owns_governance_there_not_this_object(monkeypatch):
    from computecoin_agent import platform as plat

    monkeypatch.setattr(plat, "get_platform", lambda: types.SimpleNamespace(name=plat.PLATFORM_WINDOWS))
    assert cli._owner_governance(types.SimpleNamespace(governance_enabled=True)) is None


def test_pause_and_resume_commands_write_and_remove_the_marker(tmp_path, monkeypatch):
    from computecoin_agent import owner_governance

    marker = tmp_path / "cfg" / "owner-stop"
    monkeypatch.setattr(owner_governance, "owner_stop_path", lambda config_dir=None: marker)
    runner = CliRunner()

    paused = runner.invoke(cli.app, ["pause"])
    assert paused.exit_code == 0 and marker.exists() and "paused" in paused.output.lower()

    resumed = runner.invoke(cli.app, ["resume"])
    assert resumed.exit_code == 0 and not marker.exists() and "resume" in resumed.output.lower()

    again = runner.invoke(cli.app, ["resume"])
    assert again.exit_code == 0 and "not paused" in again.output.lower()
