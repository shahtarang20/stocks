"""Run NVIDIA cards a little cooler by default, never raise a limit, and put everything back."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from computecoin_agent import cli
from computecoin_agent.config import AgentConfig
from computecoin_agent.thermal import PowerCapResult, ThermalGuard, apply_power_percent, restore_power_limits


class FakeNvml:
    """A tiny NVML: each card has (default, current, (min, max)) in milliwatts."""

    def __init__(self, cards, init_error=None, refuse=()):
        self.cards = cards
        self.set_calls = []
        self.init_error = init_error
        self.refuse = set(refuse)
        self.inits = self.shutdowns = 0

    def nvmlInit(self):
        self.inits += 1
        if self.init_error:
            raise self.init_error

    def nvmlShutdown(self):
        self.shutdowns += 1

    def nvmlDeviceGetCount(self):
        return len(self.cards)

    def nvmlDeviceGetHandleByIndex(self, i):
        return i

    def nvmlDeviceGetPowerManagementDefaultLimit(self, h):
        return self.cards[h][0]

    def nvmlDeviceGetPowerManagementLimit(self, h):
        return self.cards[h][1]

    def nvmlDeviceGetPowerManagementLimitConstraints(self, h):
        return self.cards[h][2]

    def nvmlDeviceSetPowerManagementLimit(self, h, mw):
        if h in self.refuse:
            raise PermissionError("not permitted")
        self.set_calls.append((h, mw))
        d, _c, rng = self.cards[h]
        self.cards[h] = (d, mw, rng)


CARD = (300_000, 300_000, (100_000, 350_000))


def test_a_card_is_set_to_the_percentage_of_its_own_default():
    n = FakeNvml([CARD, (200_000, 200_000, (100_000, 250_000))])
    r = apply_power_percent(90, nvml_module=n)
    assert n.set_calls == [(0, 270_000), (1, 180_000)]  # each card against ITS default, not one fixed number of watts
    assert r.changed == {0: (300_000, 270_000), 1: (200_000, 180_000)}
    assert n.inits == n.shutdowns == 1


def test_a_limit_the_owner_already_set_lower_is_never_raised():
    n = FakeNvml([(300_000, 200_000, (100_000, 350_000))])
    r = apply_power_percent(90, nvml_module=n)
    assert n.set_calls == [] and r.changed == {}


def test_the_target_stays_inside_what_the_card_allows():
    n = FakeNvml([(300_000, 300_000, (290_000, 350_000))])  # the card refuses to go below 290 W
    apply_power_percent(80, nvml_module=n)
    assert n.set_calls == [(0, 290_000)]


@pytest.mark.parametrize("percent", [None, 100, 100.0, 120])
def test_off_means_nothing_is_touched(percent):
    n = FakeNvml([CARD])
    r = apply_power_percent(percent, nvml_module=n)
    assert n.set_calls == [] and r.changed == {} and n.inits == 0


@pytest.mark.parametrize("percent", [0, 10, 49.9, -5])
def test_an_unsafe_low_percentage_is_refused_not_applied(percent):
    n = FakeNvml([CARD])
    r = apply_power_percent(percent, nvml_module=n)
    assert n.set_calls == [] and "range" in r.reason


def test_no_permission_or_no_driver_is_reported_not_raised():
    r = apply_power_percent(90, nvml_module=FakeNvml([CARD], refuse=[0]))
    assert r.changed == {} and "not permitted" in r.reason
    r = apply_power_percent(90, nvml_module=FakeNvml([CARD], init_error=RuntimeError("no driver")))
    assert r.changed == {} and "NVML unavailable" in r.reason


def test_one_refusing_card_does_not_stop_the_others():
    n = FakeNvml([CARD, CARD], refuse=[0])
    r = apply_power_percent(90, nvml_module=n)
    assert list(r.changed) == [1]


def test_everything_is_put_back_exactly():
    n = FakeNvml([CARD, (200_000, 150_000, (100_000, 250_000))])
    r = apply_power_percent(90, nvml_module=n)
    assert restore_power_limits(r, nvml_module=n) == len(r.changed)
    assert [c[1] for c in n.cards] == [300_000, 150_000]  # the second card keeps the owner's own lower limit


def test_restoring_nothing_or_failing_to_restore_is_harmless():
    assert restore_power_limits(None) == 0
    assert restore_power_limits(PowerCapResult()) == 0
    n = FakeNvml([CARD])
    r = apply_power_percent(90, nvml_module=n)
    n.refuse.add(0)
    assert restore_power_limits(r, nvml_module=n) == 0  # no raise


# ---- config --------------------------------------------------------------------------------------------------------
def test_the_default_is_on_at_90_percent_and_bad_values_are_refused():
    assert AgentConfig().gpu_power_limit_percent == 90.0
    assert AgentConfig(gpu_power_limit_percent=None).gpu_power_limit_percent is None
    assert AgentConfig(gpu_power_limit_percent=100).gpu_power_limit_percent == 100
    for bad in (0, 49, 101, -1):
        with pytest.raises(ValueError):
            AgentConfig(gpu_power_limit_percent=bad)


# ---- the agent's start-up ------------------------------------------------------------------------------------------
import atexit  # noqa: E402

from computecoin_agent.thermal import power_cap_from_json, power_cap_to_json, real_samples_only  # noqa: E402


class Signals:
    def __init__(self):
        self.handler = None
        self.restored = 0

    def install(self, handler):
        self.handler = handler

    def restore(self):
        self.restored += 1


def _stub(monkeypatch, tmp_path, result, leftover_restore=None):
    """Replace NVML, the platform and the state file (never the real home directory) and capture atexit."""
    from computecoin_agent import platform as plat
    from computecoin_agent import thermal

    monkeypatch.setattr(thermal, "apply_power_percent", lambda pct: result)
    restored = []
    monkeypatch.setattr(thermal, "restore_power_limits", lambda r: restored.append(r) or len(r.changed))
    signals = Signals()
    monkeypatch.setattr(plat, "get_platform", lambda: SimpleNamespace(signals=signals))
    state = tmp_path / "power_cap_state.json"
    monkeypatch.setattr(cli, "_power_state_file", lambda: state)
    exits = []
    monkeypatch.setattr(atexit, "register", exits.append)
    return restored, signals, state, exits


def test_start_applies_tells_the_host_and_restores_on_stop(monkeypatch, tmp_path):
    result = PowerCapResult(changed={0: (300_000, 270_000)})
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, result)
    said = []
    restore = cli._apply_default_power_cap(AgentConfig(), said.append)
    assert any("limited to 90% of its default" in m and "100 to turn this off" in m for m in said)
    assert state.exists() and power_cap_from_json(state.read_text()).changed == result.changed
    with pytest.raises(SystemExit):
        signals.handler("SIGTERM")  # systemd stopping the agent becomes a graceful stop
    restore()
    assert restored == [result] and any("put back" in m for m in said)
    assert not state.exists() and signals.restored == 1


def test_restoring_twice_does_it_once(monkeypatch, tmp_path):
    result = PowerCapResult(changed={0: (300_000, 270_000)})
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, result)
    restore = cli._apply_default_power_cap(AgentConfig(), lambda m: None)
    restore()
    restore()
    assert len(restored) == 1


def test_it_is_also_restored_when_the_process_exits_during_start_up(monkeypatch, tmp_path):
    result = PowerCapResult(changed={0: (300_000, 270_000)})
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, result)
    restore = cli._apply_default_power_cap(AgentConfig(), lambda m: None)
    assert exits == [restore]  # registered to run at exit, not only inside the main loop's finally


def test_a_second_signal_while_putting_the_limits_back_is_ignored(monkeypatch, tmp_path):
    result = PowerCapResult(changed={0: (300_000, 270_000)})
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, result)
    handler_calls = []

    def slow_restore(r):
        signals.handler("SIGINT")  # the host presses Ctrl-C again in the middle of the restore
        handler_calls.append(1)
        return 1

    from computecoin_agent import thermal

    monkeypatch.setattr(thermal, "restore_power_limits", slow_restore)
    cli._apply_default_power_cap(AgentConfig(), lambda m: None)()
    assert handler_calls == [1]  # the restore ran to the end: the extra signal did not raise


def test_a_cap_a_crashed_run_left_behind_is_undone_by_the_next_start(monkeypatch, tmp_path):
    new = PowerCapResult(changed={0: (300_000, 270_000)})
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, new)
    state.write_text(power_cap_to_json(PowerCapResult(changed={0: (280_000, 252_000)})))  # written by the crashed run
    said = []
    cli._apply_default_power_cap(AgentConfig(), said.append)
    assert restored[0].changed == {0: (280_000, 252_000)}  # the OLD run's limits were put back first
    assert any("earlier run left" in m for m in said)
    assert power_cap_from_json(state.read_text()).changed == new.changed  # and the state file now describes THIS run


def test_an_unreadable_state_file_means_nothing_to_undo(monkeypatch, tmp_path):
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, PowerCapResult(reason="off"))
    state.write_text("not json {{{")
    cli._apply_default_power_cap(AgentConfig(), lambda m: None)()
    assert restored == [] and not state.exists()


def test_an_explicit_watt_limit_wins_and_this_does_nothing(monkeypatch, tmp_path):
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, PowerCapResult(changed={0: (1, 2)}))
    said = []
    cli._apply_default_power_cap(AgentConfig(gpu_power_limit_w=200), said.append)()
    assert said == [] and signals.handler is None and restored == [] and not state.exists()


def test_when_nothing_changed_nothing_is_installed_or_restored(monkeypatch, tmp_path):
    restored, signals, state, exits = _stub(monkeypatch, tmp_path, PowerCapResult(reason="not permitted: PermissionError"))
    said = []
    cli._apply_default_power_cap(AgentConfig(), said.append)()
    assert signals.handler is None and restored == [] and not state.exists() and exits == []
    assert any("not applied" in m and "not permitted" in m for m in said)  # the host is told why, once


def test_off_is_silent(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, PowerCapResult(reason="off"))
    said = []
    cli._apply_default_power_cap(AgentConfig(gpu_power_limit_percent=None), said.append)()
    assert said == []


def test_a_failure_never_stops_the_agent(monkeypatch, tmp_path):
    from computecoin_agent import thermal

    _stub(monkeypatch, tmp_path, PowerCapResult())

    def boom(pct):
        raise RuntimeError("x")

    monkeypatch.setattr(thermal, "apply_power_percent", boom)
    said = []
    cli._apply_default_power_cap(AgentConfig(), said.append)()
    assert any("skipped" in m for m in said)


# ---- a limit is only put back on a card that is still at OUR cap ----------------------------------------------------
def test_a_card_someone_else_changed_since_is_left_alone():
    n = FakeNvml([CARD])
    r = apply_power_percent(90, nvml_module=n)  # we set 270 W
    n.cards[0] = (300_000, 250_000, n.cards[0][2])  # the owner then set 250 W
    assert restore_power_limits(r, nvml_module=n) == 0
    assert n.cards[0][1] == 250_000


def test_the_state_file_round_trips_and_garbage_is_nothing():
    r = PowerCapResult(changed={0: (300_000, 270_000), 2: (200_000, 180_000)})
    assert power_cap_from_json(power_cap_to_json(r)).changed == r.changed
    for bad in ("", "nope", "[]", '{"x": [1,2]}', '{"0": [1]}', "null"):
        assert power_cap_from_json(bad).changed == {}


# ---- the fake "everything is fine" reading must never reach the thermal guard ---------------------------------------
def test_the_fake_probe_sample_is_dropped():
    fake = SimpleNamespace(temp_c=48.5, extra_metrics={"mode": "simulated_probe"})
    real = SimpleNamespace(temp_c=71.0, extra_metrics=None)
    assert real_samples_only([fake, real]) == [real]
    assert real_samples_only([fake]) == []
    assert real_samples_only(None) == []


def test_the_default_sampler_gives_no_reading_when_nothing_is_readable(monkeypatch):
    from computecoin_agent import telemetry, thermal

    monkeypatch.setattr(telemetry, "sample_gpu_metrics", lambda simulated=False: [
        SimpleNamespace(temp_c=48.5, extra_metrics={"mode": "simulated_probe"})])
    assert thermal._default_sampler() == []
    assert ThermalGuard().read_max_temp() is None  # a driver that stopped answering is NOT a cool 48.5 C
    assert ThermalGuard().has_sensor() is False
