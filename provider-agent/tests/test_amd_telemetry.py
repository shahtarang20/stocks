"""AMD GPU telemetry (telemetry.py, WP3), against the amdsmi API as AMD documents it for ROCm 6.x.

The first version of these tests mocked functions amdsmi does not have
(amdsmi_get_gpu_temperature, amdsmi_get_gpu_power_info), read VRAM "used" from vram_info (which has
no such field) and treated MB values as bytes, so they passed while a real AMD GPU would report no
temperature, no power, no memory use and 0 MB of VRAM.
"""

from __future__ import annotations

import types

from computecoin_agent.telemetry import sample_gpu_metrics


class FakeHandle:
    def __init__(self, idx: int):
        self.idx = idx


def fake_amdsmi(*, activity=None, total_b=None, used_b=None, temp=None, power=None, fan=None, fan_max=255, vram_usage=None):
    mod = types.ModuleType("amdsmi")
    mod.calls = []
    mod.AmdSmiMemoryType = types.SimpleNamespace(VRAM="VRAM")
    mod.AmdSmiTemperatureType = types.SimpleNamespace(EDGE="EDGE")
    mod.AmdSmiTemperatureMetric = types.SimpleNamespace(CURRENT="CURRENT")
    mod.amdsmi_init = lambda: mod.calls.append("init")
    mod.amdsmi_shut_down = lambda: mod.calls.append("shut_down")
    mod.amdsmi_get_processor_handles = lambda: [FakeHandle(0)]
    mod.amdsmi_get_gpu_activity = lambda h: activity if activity is not None else {"gfx_activity": "N/A", "umc_activity": "N/A", "mm_activity": "N/A"}

    def _total(h, t):
        assert t == "VRAM"
        if total_b is None:
            raise RuntimeError("not supported")
        return total_b

    def _used(h, t):
        assert t == "VRAM"
        if used_b is None:
            raise RuntimeError("not supported")
        return used_b

    def _temp(h, sensor, metric):
        assert (sensor, metric) == ("EDGE", "CURRENT")
        if temp is None:
            raise RuntimeError("not supported")
        return temp

    mod.amdsmi_get_gpu_memory_total = _total
    mod.amdsmi_get_gpu_memory_usage = _used
    if vram_usage is not None:
        mod.amdsmi_get_gpu_vram_usage = lambda h: vram_usage
    mod.amdsmi_get_temp_metric = _temp
    mod.amdsmi_get_power_info = lambda h: power if power is not None else {"socket_power": "N/A"}
    mod.amdsmi_get_gpu_fan_speed = lambda h, i: fan if fan is not None else "N/A"
    mod.amdsmi_get_gpu_fan_speed_max = lambda h, i: fan_max
    return mod


def test_amd_telemetry_full_readings():
    mod = fake_amdsmi(activity={"gfx_activity": 78, "umc_activity": 10, "mm_activity": 0},
                      total_b=24576 * 1024 * 1024, used_b=12288 * 1024 * 1024,
                      temp=64500, power={"socket_power": 240, "gfx_voltage": 900}, fan=153, fan_max=255)
    (s,) = sample_gpu_metrics(amdsmi_module=mod, simulated=False)
    assert s.gpu_util_pct == 78.0
    assert (s.mem_total_mb, s.mem_used_mb, s.mem_util_pct) == (24576, 12288, 50.0)
    assert s.temp_c == 64.5  # millidegrees -> degrees
    assert s.power_w == 240.0
    assert s.fan_speed_pct == 60.0  # 153 of max 255
    assert s.extra_metrics is None
    assert mod.calls == ["init", "shut_down"]  # every sample closes its amdsmi session


def test_amd_telemetry_unexposed_readings_are_none():
    """D9: what an Instinct board does not expose ('N/A', unsupported) is None, never 0 or invented."""
    mod = fake_amdsmi()
    (s,) = sample_gpu_metrics(amdsmi_module=mod, simulated=False)
    for value in (s.gpu_util_pct, s.mem_used_mb, s.mem_total_mb, s.mem_util_pct, s.temp_c, s.power_w, s.fan_speed_pct):
        assert value is None
    assert s.extra_metrics is None


def test_mi300_power_key_and_degrees_already_in_celsius():
    mod = fake_amdsmi(power={"socket_power": "N/A", "current_socket_power": 550}, temp=71)
    (s,) = sample_gpu_metrics(amdsmi_module=mod, simulated=False)
    assert s.power_w == 550.0 and s.temp_c == 71.0


def test_older_rocm_vram_usage_in_mb():
    mod = fake_amdsmi(vram_usage={"vram_total": 32768, "vram_used": 8192})
    (s,) = sample_gpu_metrics(amdsmi_module=mod, simulated=False)
    assert (s.mem_total_mb, s.mem_used_mb, s.mem_util_pct) == (32768, 8192, 25.0)


def test_amd_telemetry_failure_falls_back_to_simulated_and_still_shuts_down():
    mod = fake_amdsmi()

    def boom():
        raise RuntimeError("driver bus error")

    mod.amdsmi_get_processor_handles = boom
    samples = sample_gpu_metrics(amdsmi_module=mod, simulated=False)
    assert len(samples) == 1 and samples[0].extra_metrics == {"mode": "simulated_probe"}
    assert mod.calls == ["init", "shut_down"]
