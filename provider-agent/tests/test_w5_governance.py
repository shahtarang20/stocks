"""W5: resource governance. Policy engine defaults and hysteresis, schedules, snapshot probe, Windows probe logic with fakes.

Everything runs on Linux with fakes (no Win32). WRITTEN, NOT RUN (the task forbids running tests).
"""

from __future__ import annotations

import datetime as dt

import pytest

from computecoin_agent.platform import ActivityProbe, ActivitySnapshot, PowerSnapshot, PowerState, PlatformUnsupported
from computecoin_agent.windows.governance import snapshot as snap
from computecoin_agent.windows.governance.integration import GovernedGuard, JobTracker
from computecoin_agent.windows.governance.policy import (
    GovernanceEngine,
    GovernancePolicy,
    ProbeUnsupported,
    ScheduleWindow,
    Signals,
    State,
    collect_signals,
    parse_schedule,
)
from computecoin_agent.windows.governance import probes

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def clear(now, **kw) -> Signals:
    base = dict(owner_active=False, fullscreen=False, idle_seconds=10_000.0, foreground_gpu_app=None, on_battery=False,
                sleep_pending=False, reboot_pending=False, free_vram_mb=8000)
    base.update(kw)
    return Signals(now=now, **base)


def settled(policy=None, t=0.0) -> tuple:
    """An engine that has been quiet long enough to be online, and the time it got there."""
    engine = GovernanceEngine(policy)
    engine.evaluate(clear(at(t)))
    d = engine.evaluate(clear(at(t + engine.policy.resume_clear_seconds)))
    assert d.state is State.ONLINE
    return engine, t + engine.policy.resume_clear_seconds


# ---- defaults (X13) ---------------------------------------------------------------------------------------------------

def test_defaults_are_conservative():
    p = GovernancePolicy()
    assert p.yield_to_owner is True
    assert p.allow_on_battery is False
    assert p.idle_threshold_seconds == 300 and p.resume_clear_seconds == 60
    assert p.vram_reserve_mb > 0
    assert p.preempt_on_fullscreen is False
    assert set(p.unknown_blocks) >= {"fullscreen", "idle", "battery", "reboot"}


def test_engine_starts_closed_and_opens_only_after_a_quiet_period():
    e = GovernanceEngine()
    first = e.evaluate(clear(at(0)))
    assert first.state is State.YIELDING and first.code == "settling" and not first.claim_allowed
    assert e.evaluate(clear(at(59))).state is State.YIELDING
    assert e.evaluate(clear(at(60))).state is State.ONLINE


def test_config_defaults_match_policy_defaults():
    from computecoin_agent.config import AgentConfig

    cfg = AgentConfig()
    assert GovernancePolicy.from_config(cfg) == GovernancePolicy()
    assert cfg.governance_enabled is None and cfg.update_ring == "general"


def test_config_rejects_bad_schedule_and_ring():
    from computecoin_agent.config import AgentConfig

    with pytest.raises(ValueError):
        AgentConfig(governance_schedule=[{"days": "daily", "start": "25:00", "end": "07:00", "timezone": "UTC"}])
    with pytest.raises(ValueError):
        AgentConfig(governance_schedule=[{"days": "daily", "start": "22:00", "end": "07:00", "timezone": "Not/AZone"}])
    with pytest.raises(ValueError):
        AgentConfig(update_ring="beta")


# ---- the individual causes --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kw,code",
    [
        ({"fullscreen": True}, "fullscreen"),
        ({"foreground_gpu_app": "Game.exe"}, "foreground_gpu_app"),
        ({"idle_seconds": 12.0}, "recent_input"),
        ({"on_battery": True}, "on_battery"),
        ({"owner_active": True}, "owner_active"),
        ({"fullscreen": None}, "unknown_fullscreen"),
        ({"on_battery": None}, "unknown_battery"),
        ({"idle_seconds": None, "owner_active": None}, "unknown_idle"),
        ({"probe_errors": ("activity",)}, "probe_error"),
    ],
)
def test_each_owner_cause_stops_claims_immediately(kw, code):
    e, t = settled()
    d = e.evaluate(clear(at(t + 1), **kw))
    assert d.state is State.YIELDING and d.code == code and not d.claim_allowed


def test_idle_exactly_at_threshold_is_quiet():
    e, t = settled()
    assert e.evaluate(clear(at(t + 1), idle_seconds=300.0)).state is State.ONLINE
    assert e.evaluate(clear(at(t + 2), idle_seconds=299.0)).state is State.YIELDING


def test_ignored_desktop_processes_do_not_count_as_owner_gpu_use():
    e, t = settled()
    assert e.evaluate(clear(at(t + 1), foreground_gpu_app="DWM.EXE")).state is State.ONLINE


def test_fullscreen_blocks_even_on_a_headless_policy():
    e, t = settled(GovernancePolicy.headless())
    assert e.evaluate(clear(at(t + 1), fullscreen=True)).state is State.YIELDING


def test_headless_policy_ignores_unknown_activity_but_not_battery_or_reboot():
    e, t = settled(GovernancePolicy.headless())
    unknown = Signals(now=at(t + 1))  # a Linux server: nothing is known
    assert e.evaluate(unknown).state is State.ONLINE
    assert e.evaluate(Signals(now=at(t + 2), on_battery=True)).state is State.YIELDING
    assert e.evaluate(Signals(now=at(t + 3), reboot_pending=True)).state is State.MAINTENANCE


def test_battery_allowed_when_owner_says_so():
    e, t = settled(GovernancePolicy(allow_on_battery=True))
    assert e.evaluate(clear(at(t + 1), on_battery=True)).state is State.ONLINE


def test_yield_to_owner_off_still_refuses_fullscreen_but_allows_foreground_and_input():
    e, t = settled(GovernancePolicy(yield_to_owner=False))
    assert e.evaluate(clear(at(t + 1), foreground_gpu_app="x.exe", idle_seconds=1.0)).state is State.ONLINE
    assert e.evaluate(clear(at(t + 2), fullscreen=True)).state is State.YIELDING


def test_reboot_pending_reports_maintenance_and_drains_a_running_job():
    e, t = settled()
    idle = e.evaluate(clear(at(t + 1), reboot_pending=True))
    assert idle.state is State.MAINTENANCE and idle.code == "reboot_pending"
    e2, t2 = settled()
    busy = e2.evaluate(clear(at(t2 + 1), reboot_pending=True, job_running=True))
    assert busy.state is State.DRAINING and busy.code == "reboot_pending" and not busy.stop_job


def test_unknown_reboot_state_blocks_by_default():
    e, t = settled()
    assert e.evaluate(clear(at(t + 1), reboot_pending=None)).state is State.MAINTENANCE


# ---- hysteresis ---------------------------------------------------------------------------------------------------------

def test_no_flapping_a_blip_restarts_the_quiet_period():
    e, t = settled()
    assert e.evaluate(clear(at(t + 10), fullscreen=True)).state is State.YIELDING
    assert e.evaluate(clear(at(t + 11))).state is State.YIELDING  # clear, but waiting
    assert e.evaluate(clear(at(t + 50), fullscreen=True)).state is State.YIELDING  # blip: the count restarts
    assert e.evaluate(clear(at(t + 51))).state is State.YIELDING
    assert e.evaluate(clear(at(t + 110))).state is State.YIELDING  # only 59s since the blip cleared
    assert e.evaluate(clear(at(t + 111))).state is State.ONLINE


def test_online_does_not_need_a_new_quiet_period_while_it_stays_clear():
    e, t = settled()
    for s in range(1, 20):
        assert e.evaluate(clear(at(t + s))).state is State.ONLINE


def test_clock_going_backwards_restarts_the_quiet_period():
    e = GovernanceEngine()
    e.evaluate(clear(at(1000)))
    e.evaluate(clear(at(900)))  # the clock jumped back (resume from sleep, NTP step)
    assert e.evaluate(clear(at(959))).state is State.YIELDING
    assert e.evaluate(clear(at(960))).state is State.ONLINE


def test_zero_resume_period_goes_online_at_once():
    e = GovernanceEngine(GovernancePolicy(resume_clear_seconds=0))
    assert e.evaluate(clear(at(0))).state is State.ONLINE


def test_since_tracks_the_state_change_not_every_sample():
    e, t = settled()
    d1 = e.evaluate(clear(at(t + 1)))
    d2 = e.evaluate(clear(at(t + 5)))
    assert d1.since == d2.since


# ---- a running job -------------------------------------------------------------------------------------------------------

def test_cause_during_a_job_drains_it_without_killing_it():
    e, t = settled()
    d = e.evaluate(clear(at(t + 1), foreground_gpu_app="game.exe", job_running=True))
    assert d.state is State.DRAINING and d.stop_job is False and d.prevent_sleep is True
    done = e.evaluate(clear(at(t + 2), foreground_gpu_app="game.exe", job_running=False))
    assert done.state is State.YIELDING and done.prevent_sleep is False


def test_draining_job_finishing_during_the_quiet_period_does_not_claim_early():
    e, t = settled()
    e.evaluate(clear(at(t + 1), fullscreen=True, job_running=True))
    d = e.evaluate(clear(at(t + 2), job_running=False))
    assert d.state is State.YIELDING and d.code == "settling"


def test_sleep_is_prevented_only_while_a_job_runs():
    e, t = settled()
    assert e.evaluate(clear(at(t + 1))).prevent_sleep is False
    assert e.evaluate(clear(at(t + 2), job_running=True)).prevent_sleep is True
    assert e.evaluate(clear(at(t + 3))).prevent_sleep is False
    assert e.evaluate(clear(at(t + 4), job_running=True, sleep_pending=True)).prevent_sleep is False


def test_preempt_on_fullscreen_is_off_by_default_and_stops_the_job_when_on():
    e, t = settled()
    assert e.evaluate(clear(at(t + 1), fullscreen=True, job_running=True)).stop_job is False
    e2, t2 = settled(GovernancePolicy(preempt_on_fullscreen=True))
    assert e2.evaluate(clear(at(t2 + 1), fullscreen=True, job_running=True)).stop_job is True


# ---- stop now ------------------------------------------------------------------------------------------------------------

def test_stop_now_stops_the_running_job_and_holds_until_resume():
    e, t = settled()
    e.stop_now(at(t + 1))
    d = e.evaluate(clear(at(t + 2), job_running=True))
    assert d.state is State.YIELDING and d.code == "owner_stop_now" and d.stop_job is True
    assert e.evaluate(clear(at(t + 100_000))).state is State.YIELDING  # no timeout when hold is 0
    e.resume()
    assert e.evaluate(clear(at(t + 100_001))).state is State.YIELDING  # still the normal quiet period
    assert e.evaluate(clear(at(t + 100_061))).state is State.ONLINE


def test_stop_now_with_a_hold_expires():
    e, t = settled(GovernancePolicy(stop_now_hold_seconds=600))
    e.stop_now(at(t + 1))
    assert e.evaluate(clear(at(t + 300))).code == "owner_stop_now"
    assert e.evaluate(clear(at(t + 601))).code != "owner_stop_now"


def test_stop_now_wins_over_every_other_signal():
    e, t = settled()
    e.stop_now(at(t))
    d = e.evaluate(clear(at(t + 1), reboot_pending=True, fullscreen=True))
    assert d.code == "owner_stop_now"


# ---- schedule -------------------------------------------------------------------------------------------------------------

def _win(days, start, end, tz="UTC"):
    return ScheduleWindow.parse({"days": days, "start": start, "end": end, "timezone": tz})


def test_schedule_window_inside_and_outside():
    w = _win("weekdays", "09:00", "17:00")
    assert w.contains(dt.datetime(2026, 11, 6, 12, 0, tzinfo=UTC))  # a Friday
    assert not w.contains(dt.datetime(2026, 11, 6, 17, 0, tzinfo=UTC))  # end is exclusive
    assert not w.contains(dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC))  # Saturday


def test_schedule_window_crossing_midnight_belongs_to_its_start_day():
    w = _win(["fri"], "22:00", "07:00")
    assert w.contains(dt.datetime(2026, 11, 6, 23, 0, tzinfo=UTC))  # Fri 23:00
    assert w.contains(dt.datetime(2026, 11, 7, 6, 59, tzinfo=UTC))  # Sat 06:59, still Friday's window
    assert not w.contains(dt.datetime(2026, 11, 7, 7, 0, tzinfo=UTC))
    assert not w.contains(dt.datetime(2026, 11, 6, 6, 0, tzinfo=UTC))  # Fri 06:00 is Thursday's window, not enabled


def test_schedule_uses_the_windows_own_timezone_and_dst():
    berlin = _win("daily", "22:00", "23:00", "Europe/Berlin")
    assert berlin.contains(dt.datetime(2026, 1, 15, 21, 30, tzinfo=UTC))  # CET = UTC+1
    assert not berlin.contains(dt.datetime(2026, 7, 15, 21, 30, tzinfo=UTC))  # CEST = UTC+2: that is 23:30 local
    assert berlin.contains(dt.datetime(2026, 7, 15, 20, 30, tzinfo=UTC))


def test_schedule_with_unknown_zone_never_matches():
    w = ScheduleWindow(days=frozenset(range(7)), start=dt.time(0, 0), end=dt.time(23, 59), timezone="Not/AZone")
    assert w.contains(T0) is False


def test_schedule_parse_refuses_typos():
    for bad in (
        {"days": "daily", "start": "9", "end": "17:00", "timezone": "UTC"},
        {"days": ["funday"], "start": "09:00", "end": "17:00", "timezone": "UTC"},
        {"days": [], "start": "09:00", "end": "17:00", "timezone": "UTC"},
        {"days": "daily", "start": "09:00", "end": "17:00"},
        {"days": "daily", "start": "09:00", "end": "17:00", "timezone": "UTC", "extra": 1},
        {"days": [9], "start": "09:00", "end": "17:00", "timezone": "UTC"},
    ):
        with pytest.raises(ValueError):
            ScheduleWindow.parse(bad)


def test_outside_schedule_yields_and_inside_is_online():
    policy = GovernancePolicy(schedule=parse_schedule([{"days": "daily", "start": "13:00", "end": "14:00", "timezone": "UTC"}]))
    e = GovernanceEngine(policy)
    assert e.evaluate(clear(at(0))).code == "outside_schedule"  # 12:00 UTC
    e2, t = settled(GovernancePolicy(schedule=parse_schedule([{"days": "daily", "start": "11:00", "end": "14:00", "timezone": "UTC"}])))
    assert e2.evaluate(clear(at(t + 1))).state is State.ONLINE


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        GovernanceEngine().evaluate(Signals(now=dt.datetime(2026, 1, 1)))


# ---- VRAM reserve ----------------------------------------------------------------------------------------------------------

def test_vram_reserve_blocks_claims_when_the_owner_needs_the_memory():
    e, t = settled(GovernancePolicy(vram_reserve_mb=2048))
    assert e.evaluate(clear(at(t + 1), free_vram_mb=1000)).code == "vram_reserve"


def test_own_job_memory_does_not_make_the_host_yield_to_itself():
    e, t = settled(GovernancePolicy(vram_reserve_mb=2048))
    assert e.evaluate(clear(at(t + 1), free_vram_mb=100, job_running=True)).state is State.ONLINE


def test_fits_vram_keeps_the_reserve_and_refuses_unknown():
    e = GovernanceEngine(GovernancePolicy(vram_reserve_mb=1024))
    assert e.fits_vram(9100, 8000) and not e.fits_vram(9000, 8000)
    assert e.fits_vram(None, 1) is False and e.fits_vram(9000, -1) is False


# ---- heartbeat mapping ------------------------------------------------------------------------------------------------------

def test_heartbeat_never_reports_the_sticky_maintenance_value():
    e, t = settled()
    for kw in ({"reboot_pending": True}, {"fullscreen": True}, {"fullscreen": True, "job_running": True}):
        d = e.evaluate(clear(at(t + 1), **kw))
        assert d.heartbeat_availability == "throttled"
        assert d.heartbeat_health()["availability_detail"]["state"] in {s.value for s in State}
    ok, t2 = settled()
    assert ok.evaluate(clear(at(t2 + 1))).heartbeat_availability == "online"


# ---- collecting signals from the PAL interfaces --------------------------------------------------------------------------------

class FakeActivity(ActivityProbe):
    def __init__(self, snap=None, exc=None):
        self.snap, self.exc = snap, exc

    def snapshot(self):
        if self.exc:
            raise self.exc
        return self.snap


class FakePower(PowerState):
    def __init__(self, snap=None, exc=None):
        self.snap, self.exc = snap, exc

    def snapshot(self):
        if self.exc:
            raise self.exc
        return self.snap


class FakeGpu:
    def __init__(self, app=None, free=8000, app_exc=None):
        self.app, self.free, self.app_exc = app, free, app_exc

    def foreground_gpu_app(self):
        if self.app_exc:
            raise self.app_exc
        return self.app

    def free_vram_mb(self):
        return self.free


def test_collect_signals_reads_the_pal_snapshots():
    s = collect_signals(
        T0,
        activity=FakeActivity(ActivitySnapshot(owner_active=False, fullscreen_app=True, idle_seconds=42.0)),
        power=FakePower(PowerSnapshot(on_battery=True, sleep_pending=False, reboot_pending=True)),
        owner_gpu=FakeGpu(app="game.exe", free=512),
        job_running=True,
    )
    assert (s.fullscreen, s.idle_seconds, s.on_battery, s.reboot_pending) == (True, 42.0, True, True)
    assert (s.foreground_gpu_app, s.free_vram_mb, s.job_running, s.probe_errors) == ("game.exe", 512, True, ())


def test_unsupported_probe_is_unknown_and_a_crashing_probe_blocks():
    s = collect_signals(T0, activity=FakeActivity(exc=PlatformUnsupported("x")), power=FakePower(PowerSnapshot()),
                        owner_gpu=FakeGpu(app_exc=ProbeUnsupported("no nvml")))
    assert s.fullscreen is None and s.probe_errors == ()
    boom = collect_signals(T0, activity=FakeActivity(exc=RuntimeError("driver crashed")), power=FakePower(PowerSnapshot()))
    assert boom.probe_errors == ("activity",)
    e, t = settled()
    assert e.evaluate(clear(at(t + 1), probe_errors=boom.probe_errors)).code == "probe_error"


def test_linux_default_pal_probes_give_all_unknown_so_the_default_policy_blocks():
    s = collect_signals(T0, activity=ActivityProbe(), power=PowerState())
    assert s.fullscreen is None and s.on_battery is None and s.reboot_pending is None
    assert GovernanceEngine().evaluate(s).claim_allowed is False


# ---- session snapshot probe ----------------------------------------------------------------------------------------------------

def _line(**kw):
    base = dict(taken_at=1000.0, fullscreen=False, idle_seconds=5.0, foreground_gpu_app=None, free_vram_mb=4000)
    base.update(kw)
    return snap.encode_snapshot(**base)


def test_snapshot_probe_reads_a_fresh_report():
    p = snap.SnapshotProbe(lambda: _line(fullscreen=True, foreground_gpu_app="g.exe"), clock=lambda: 1010.0)
    a = p.snapshot()
    assert a.fullscreen_app is True and a.idle_seconds == 5.0
    assert p.foreground_gpu_app() == "g.exe" and p.free_vram_mb() == 4000


def test_snapshot_probe_fails_closed_on_stale_missing_or_future_reports():
    for source, now in ((lambda: _line(), 1100.0), (lambda: None, 1000.0), (lambda: _line(taken_at=2000.0), 1000.0)):
        with pytest.raises(snap.SnapshotError):
            snap.SnapshotProbe(source, max_age_seconds=30, clock=lambda n=now: n).snapshot()
    probe = snap.SnapshotProbe(lambda: None, clock=lambda: 1000.0)
    s = collect_signals(T0, activity=probe)
    assert s.probe_errors == ("activity",)


def test_snapshot_probe_with_no_logged_in_user_means_the_owner_is_away():
    p = snap.SnapshotProbe(lambda: None, no_session=lambda: True)
    a = p.snapshot()
    assert a.fullscreen_app is False and a.idle_seconds > 1e6 and p.foreground_gpu_app() is None


def test_snapshot_decoding_is_strict():
    for bad in ("not json", "[]", '{"v":2}', _line().replace('"v":1', '"v":1,"extra":1'),
                _line(idle_seconds=-1.0), _line(free_vram_mb=True), _line(fullscreen="yes")):
        with pytest.raises(snap.SnapshotError):
            snap.decode_snapshot(bad)


# ---- Windows probe logic with a fake Win32 ---------------------------------------------------------------------------------------

class FakeApi:
    def __init__(self, **kw):
        self.kw = kw
        self.exec_calls = []

    def idle_ms(self):
        return self.kw.get("idle_ms", 1000)

    def notification_state(self):
        return self.kw.get("notification_state")

    def foreground_window(self):
        return self.kw.get("window")

    def process_name(self, pid):
        return self.kw.get("process", "game.exe")

    def power_status(self):
        return self.kw.get("power", (1, 0))

    def set_execution_state(self, flags):
        self.exec_calls.append(flags)
        return self.kw.get("exec_result", 0x80000000)

    def active_console_session(self):
        return self.kw.get("console", 1)


MON = (0, 0, 1920, 1080)


def win(rect=MON, cls="UnrealWindow", shell=False, pid=77):
    return probes.WindowInfo(hwnd=1, pid=pid, class_name=cls, rect=rect, monitor_rect=MON, is_shell=shell)


def test_fullscreen_by_shell_notification_state():
    for state in (2, 3, 4):
        assert probes.WindowsActivityProbe(FakeApi(notification_state=state)).fullscreen() is True


def test_fullscreen_by_geometry_but_not_for_a_maximised_window_or_the_desktop():
    assert probes.WindowsActivityProbe(FakeApi(notification_state=5, window=win())).fullscreen() is True
    assert probes.WindowsActivityProbe(FakeApi(notification_state=5, window=win(rect=(0, 0, 1920, 1040)))).fullscreen() is False
    assert probes.WindowsActivityProbe(FakeApi(notification_state=5, window=win(cls="Progman"))).fullscreen() is False
    assert probes.WindowsActivityProbe(FakeApi(notification_state=5, window=win(shell=True))).fullscreen() is False


def test_fullscreen_unknown_when_nothing_can_be_read():
    assert probes.WindowsActivityProbe(FakeApi(notification_state=None, window=None)).fullscreen() is None


def test_idle_seconds_from_milliseconds():
    assert probes.WindowsActivityProbe(FakeApi(idle_ms=2500)).snapshot().idle_seconds == 2.5


def test_foreground_gpu_app_needs_the_foreground_pid_in_the_nvml_list():
    api = FakeApi(window=win(pid=77), process="Game.exe")
    p = probes.WindowsOwnerGpuProbe(api=api, gpu_pids=lambda: {77: 2048}, free_vram=lambda: 5000)
    assert p.foreground_gpu_app() == "Game.exe" and p.free_vram_mb() == 5000
    other = probes.WindowsOwnerGpuProbe(api=api, gpu_pids=lambda: {99: 100})
    assert other.foreground_gpu_app() is None
    shell = probes.WindowsOwnerGpuProbe(api=FakeApi(window=win(shell=True)), gpu_pids=lambda: {77: 1})
    assert shell.foreground_gpu_app() is None


class FakeRegistry:
    def __init__(self, keys=(), rename=False, exc=None):
        self.keys, self.rename, self.exc = set(keys), rename, exc

    def key_exists(self, path):
        if self.exc:
            raise self.exc
        return path in self.keys

    def value_nonempty(self, path, name):
        return self.rename


def test_reboot_pending_from_registry_keys():
    k = probes.REBOOT_KEYS
    assert probes.WindowsPowerState(FakeApi(), FakeRegistry({k[0]})).reboot_pending() is True
    assert probes.WindowsPowerState(FakeApi(), FakeRegistry({k[1]})).reboot_pending() is True
    assert probes.WindowsPowerState(FakeApi(), FakeRegistry()).reboot_pending() is False
    # the noisy value is opt-in
    assert probes.WindowsPowerState(FakeApi(), FakeRegistry(rename=True)).reboot_pending() is False
    assert probes.WindowsPowerState(FakeApi(), FakeRegistry(rename=True), include_pending_file_rename=True).reboot_pending() is True
    # an unreadable registry is UNKNOWN, never "no reboot pending"
    assert probes.WindowsPowerState(FakeApi(), FakeRegistry(exc=OSError("denied"))).reboot_pending() is None


def test_battery_state():
    on = lambda ac, flag=0: probes.WindowsPowerState(FakeApi(power=(ac, flag)), FakeRegistry()).on_battery()
    assert on(0) is True and on(1) is False and on(255) is None
    assert on(0, probes.BATTERY_FLAG_NO_BATTERY) is False  # a desktop PC has no battery to be on


def test_suspend_latch_follows_power_broadcasts():
    latch = probes.PowerEventLatch()
    assert latch.pending is False
    latch.handle_power_event(probes.PBT_APMSUSPEND)
    assert latch.pending is True
    latch.handle_power_event(probes.PBT_APMRESUMEAUTOMATIC)
    assert latch.pending is False
    ps = probes.WindowsPowerState(FakeApi(), FakeRegistry(), latch)
    latch.handle_power_event(probes.PBT_APMSUSPEND)
    assert ps.snapshot().sleep_pending is True


def test_sleep_inhibitor_requests_only_while_held_and_clears_after():
    api = FakeApi()
    inh = probes.SleepInhibitor(api)
    with inh.hold():
        assert inh.held is True
        assert api.exec_calls == [probes.ES_CONTINUOUS | probes.ES_SYSTEM_REQUIRED]
    assert api.exec_calls[-1] == probes.ES_CONTINUOUS  # cleared: back to the normal idle timeout
    assert not (api.exec_calls[-1] & probes.ES_SYSTEM_REQUIRED)
    assert inh.held is False


def test_sleep_inhibitor_never_asks_for_the_display_and_counts_nested_holds():
    api = FakeApi()
    inh = probes.SleepInhibitor(api)
    inh.acquire()
    inh.acquire()
    inh.release()
    assert len(api.exec_calls) == 1 and inh.held is True  # one request, still held
    inh.release()
    assert len(api.exec_calls) == 2
    assert all(not (c & 0x2) for c in api.exec_calls)  # ES_DISPLAY_REQUIRED (0x2) is never set


def test_sleep_inhibitor_reports_failure_and_follows_the_decision():
    api = FakeApi(exec_result=0)
    inh = probes.SleepInhibitor(api)
    inh.acquire()
    assert inh.held is False
    inh.release()
    api2 = FakeApi()
    inh2 = probes.SleepInhibitor(api2)
    inh2.sync(True)
    inh2.sync(True)
    assert len(api2.exec_calls) == 1
    inh2.sync(False)
    assert api2.exec_calls[-1] == probes.ES_CONTINUOUS


def test_win32_api_refuses_to_exist_off_windows():
    import sys

    if sys.platform != "win32":
        with pytest.raises(ProbeUnsupported):
            probes.Win32Api()


def test_take_snapshot_marks_failures_unknown_not_false():
    class Boom:
        def fullscreen(self):
            raise RuntimeError("x")

        def idle_seconds(self):
            return 3.0

    class Gpu:
        def foreground_gpu_app(self):
            return None

        def free_vram_mb(self):
            raise RuntimeError("nvml")

    data = snap.decode_snapshot(probes.take_snapshot(Boom(), Gpu(), clock=lambda: 5.0))
    assert data["fullscreen"] is None and data["free_vram_mb"] is None and data["idle_seconds"] == 3.0


# ---- the guard that plugs into the existing claim loop ---------------------------------------------------------------------------------

class FakeThermal:
    def __init__(self, ok=True):
        self.ok = ok
        self.cap_c = 85.0

    def may_claim(self):
        return self.ok

    def availability(self):
        return "online" if self.ok else "throttled"

    def read_max_temp(self):
        return 60.0


def _guard(thermal=None, **sig):
    engine = GovernanceEngine(GovernancePolicy(resume_clear_seconds=0))
    clock_now = [T0]

    def collect(now, job_running):
        return clear(now, job_running=job_running, **sig)

    g = GovernedGuard(engine, collect, thermal=thermal, clock=lambda: clock_now[0])
    return g


def test_guard_needs_both_governance_and_thermal_to_allow_a_claim():
    assert _guard(FakeThermal(True)).may_claim() is True
    assert _guard(FakeThermal(False)).may_claim() is False
    assert _guard(FakeThermal(True), fullscreen=True).may_claim() is False
    assert _guard(FakeThermal(True), fullscreen=True).availability() == "throttled"


def test_guard_delegates_the_thermal_surface_the_executor_uses():
    g = _guard(FakeThermal())
    assert g.read_max_temp() == 60.0 and g.cap_c == 85.0
    with pytest.raises(AttributeError):
        _guard(None).read_max_temp()


def test_guard_blocks_when_signal_collection_itself_breaks():
    engine = GovernanceEngine(GovernancePolicy(resume_clear_seconds=0))

    def broken(now, job_running):
        raise RuntimeError("collector bug")

    assert GovernedGuard(engine, broken, clock=lambda: T0).may_claim() is False


def test_guard_calls_the_stop_callback_only_for_stop_now():
    stopped = []
    engine = GovernanceEngine(GovernancePolicy(resume_clear_seconds=0))
    g = GovernedGuard(engine, lambda now, job: clear(now, job_running=job), on_stop_job=stopped.append, clock=lambda: T0)
    g.jobs.begin()
    g.decide()
    assert stopped == []
    engine.stop_now(T0)
    g.decide()
    assert len(stopped) == 1 and stopped[0].code == "owner_stop_now"


def test_job_tracker_counts():
    jt = JobTracker()
    assert jt.running is False
    jt.begin()
    jt.begin()
    jt.end()
    assert jt.running is True
    jt.end()
    jt.end()
    assert jt.running is False


def test_make_collector_feeds_the_guard_from_pal_probes():
    from computecoin_agent.windows.governance.integration import make_collector

    collect = make_collector(
        activity=FakeActivity(ActivitySnapshot(owner_active=False, fullscreen_app=True, idle_seconds=9999.0)),
        power=FakePower(PowerSnapshot(on_battery=False, sleep_pending=False, reboot_pending=False)),
    )
    engine = GovernanceEngine(GovernancePolicy(resume_clear_seconds=0))
    g = GovernedGuard(engine, collect, clock=lambda: T0)
    assert g.may_claim() is False and g.engine.last().code == "fullscreen"
