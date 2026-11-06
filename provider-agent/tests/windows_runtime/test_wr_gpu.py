"""W7 GPU helpers and config-file helpers (written, never run)."""

from __future__ import annotations

import types

import pytest

from computecoin_agent.telemetry import TelemetrySample
from computecoin_agent.windows.runtime import gpu
from computecoin_agent.windows.runtime.config_files import (
    ResourceLimits, desired_wslconfig, merge_wslconfig, remove_managed, render_slice,
)
from computecoin_agent.windows.runtime.wsl import RunResult, WslCli

from .fakes import IMG, FakeWsl


# ---- native NVML thermal through the existing ThermalGuard ------------------------------------------------------------- #
def cfg():
    return types.SimpleNamespace(gpu_temp_cap_c=85.0, gpu_temp_resume_c=80.0, gpu_temp_hard_stop_c=95.0)


class FakeNvml:
    def __init__(self, fail_init=False):
        self.fail_init, self.inits, self.shutdowns = fail_init, 0, 0

    def nvmlInit(self):
        if self.fail_init:
            raise RuntimeError("no driver")
        self.inits += 1

    def nvmlShutdown(self):
        self.shutdowns += 1


def sampler_for(temp):
    def _s(nvml_module=None):
        return [TelemetrySample(gpu_index=0, temp_c=temp)] if temp is not None else []
    return _s


def test_probe_feeds_the_thermal_guard():
    from computecoin_agent.thermal import HARD_STOP, OK, THROTTLED

    for temp, want in ((70.0, OK), (86.0, THROTTLED), (96.0, HARD_STOP)):
        guard = gpu.thermal_guard(cfg(), gpu.NvmlProbe(FakeNvml(), sampler_for(temp)))
        assert guard.check() == want


def test_probe_initialises_and_shuts_down_nvml():
    nvml = FakeNvml()
    gpu.NvmlProbe(nvml, sampler_for(60.0)).sample()
    assert (nvml.inits, nvml.shutdowns) == (1, 1)


def test_probe_returns_nothing_when_nvml_is_unavailable_so_the_guard_never_blocks():
    probe = gpu.NvmlProbe(FakeNvml(fail_init=True), sampler_for(99.0))
    assert probe.sample() == []
    assert gpu.thermal_guard(cfg(), probe).may_claim() is True


def test_probe_drops_the_simulated_fallback_sample():
    fake = TelemetrySample(gpu_index=0, temp_c=48.5, extra_metrics={"mode": "simulated_probe"})
    probe = gpu.NvmlProbe(FakeNvml(), lambda nvml_module=None: [fake])
    assert probe.sample() == []


# ---- power limit ------------------------------------------------------------------------------------------------------- #
def smi(log, bounds="0, 100.00, 450.00\n1, 100.00, 450.00\n", fail_set=False):
    def run(argv):
        log.append(list(argv))
        if "--query-gpu=index,power.min_limit,power.max_limit" in argv:
            return RunResult(0, bounds)
        return RunResult(1, "", "Insufficient Permissions") if fail_set else RunResult(0, "Power limit set")
    return run


def test_power_limit_needs_admin_and_never_raises():
    log = []
    res = gpu.apply_power_limit(300, run=smi(log), is_admin=lambda: False)
    assert res.skipped_reason.startswith("needs_admin") and not res.applied and log == []


def test_power_limit_applies_to_each_gpu_with_nvidia_smi_pl():
    log = []
    res = gpu.apply_power_limit(300.0, run=smi(log), is_admin=lambda: True)
    assert res.applied == [0, 1] and res.ok
    assert ["nvidia-smi", "-i", "0", "-pl", "300"] in log and ["nvidia-smi", "-i", "1", "-pl", "300"] in log


def test_power_limit_outside_driver_bounds_is_refused_not_clamped():
    log = []
    res = gpu.apply_power_limit(50.0, run=smi(log), is_admin=lambda: True)
    assert not res.applied and len(res.errors) == 2 and not any("-pl" in a for a in log)


@pytest.mark.parametrize("limit", [None, 0, -5])
def test_power_limit_unset_or_invalid_is_a_no_op(limit):
    assert not gpu.apply_power_limit(limit, run=smi([]), is_admin=lambda: True).applied


def test_power_limit_refusal_by_driver_is_reported_not_raised():
    res = gpu.apply_power_limit(300, run=smi([], fail_set=True), is_admin=lambda: True)
    assert not res.ok and res.errors


def test_power_limit_exception_is_swallowed():
    def boom(argv):
        raise OSError("nvidia-smi missing")
    res = gpu.apply_power_limit(300, run=boom, is_admin=lambda: True)
    assert res.errors and not res.applied


# ---- multi-GPU device selection under WSL2 -------------------------------------------------------------------------------- #
def two_gpus():
    f = FakeWsl(gpus=[("RTX A", "GPU-0000-aaaa"), ("RTX B", "GPU-1111-bbbb")])
    f.distros["computecoin"] = {"files": {}, "users": {"ccrunner"}, "pkgs": {}, "running": True, "wheel": None}
    return f, WslCli(f, "computecoin")


def test_device_spec_lists_singles_pair_and_a_negative_case():
    ids = [(c.id, c.device_ids, c.expect_success) for c in gpu.device_selection_spec(2)]
    assert ids == [("D0", (0,), True), ("D1", (1,), True), ("P1", (0, 1), True), ("N1", (2,), False)]
    assert [c.id for c in gpu.device_selection_spec(1)] == ["D0", "N1"]


def test_device_selection_passes_when_each_index_maps_to_its_uuid():
    f, cli = two_gpus()
    checks = gpu.verify_device_selection(cli, IMG)
    assert [c.name for c in checks] == ["device_selection_D0", "device_selection_D1", "device_selection_P1", "device_selection_N1"]
    assert all(c.ok for c in checks), [c.detail for c in checks]
    sent = [p for p in f.programs() if p[:2] == ["docker", "run"]]
    assert ["--gpus", '"device=0,1"'] == sent[3][sent[3].index("--gpus"):sent[3].index("--gpus") + 2]


def test_device_selection_detects_a_silent_fallback_to_all_gpus():
    f, cli = two_gpus()
    original = f._smoke

    def always_all(a):
        a = list(a)
        a[a.index("--gpus") + 1] = "all"
        return original(a)
    f._smoke = always_all
    checks = gpu.verify_device_selection(cli, IMG)
    assert not all(c.ok for c in checks)
    assert not next(c for c in checks if c.name == "device_selection_D0").ok
    assert not next(c for c in checks if c.name == "device_selection_N1").ok


def test_device_selection_fails_when_no_gpu_is_visible():
    f, cli = two_gpus()
    f.smoke_fail = True
    assert not gpu.verify_device_selection(cli, IMG)[0].ok


def test_allocator_is_the_runners_own_class():
    from computecoin_agent.gpu_alloc import GpuAllocator

    a = gpu.build_allocator(2)
    assert isinstance(a, GpuAllocator) and a.acquire(2) == [0, 1]


# ---- capabilities: AMD and every other forbidden flag stay off ------------------------------------------------------------ #
def test_amd_and_private_tier_flags_are_never_advertised_on_windows():
    caps = gpu.runtime_capabilities(smoke_passed=True, egress_verified=True, thermal_readable=True, power_limit_applied=True,
                                    device_selection_verified=True, gpu_count=2)
    assert set(caps) == {"container_tier", "nvidia_cuda", "egress_proxy", "thermal_cap", "power_limit", "multi_gpu"}
    assert not gpu.FORBIDDEN_ON_WINDOWS.intersection(caps)
    assert {"amd_rocm", "gvisor", "microvm", "sealed", "sealed_pro", "confidential", "vfio_gpu", "multi_node"} <= gpu.FORBIDDEN_ON_WINDOWS


def test_no_capability_without_a_passed_smoke_test():
    assert gpu.runtime_capabilities(smoke_passed=False, egress_verified=True, thermal_readable=True, power_limit_applied=True,
                                    device_selection_verified=True, gpu_count=4) == []


def test_multi_gpu_needs_two_gpus_and_a_verified_selection():
    base = dict(smoke_passed=True, egress_verified=False, thermal_readable=False, power_limit_applied=False)
    assert "multi_gpu" not in gpu.runtime_capabilities(**base, device_selection_verified=True, gpu_count=1)
    assert "multi_gpu" not in gpu.runtime_capabilities(**base, device_selection_verified=False, gpu_count=2)


# ---- config files ---------------------------------------------------------------------------------------------------------- #
def test_slice_without_limits_has_no_caps():
    text = render_slice(ResourceLimits())
    assert "MemoryMax" not in text and "CPUQuota" not in text


@pytest.mark.parametrize("bad", [dict(memory_mb=100), dict(cpus=0), dict(disk_gb=5), dict(vm_memory_mb=10), dict(vm_processors=0)])
def test_limits_validation(bad):
    with pytest.raises(ValueError):
        ResourceLimits(**bad).validate()


def test_merge_adds_a_wsl2_section_when_there_is_none_and_leaves_other_lines_alone():
    original = "[user]\r\ndefault=me\r\n"
    plan = merge_wslconfig(original, desired_wslconfig(ResourceLimits(vm_memory_mb=8192)))
    assert plan.text.startswith(original.rstrip("\r\n")) and "[wsl2]" in plan.text and "memory=8192MB" in plan.text
    assert "\r\n" in plan.text and "\n" not in plan.text.replace("\r\n", "")
    assert remove_managed(plan.text) == original + "\r\n[wsl2]\r\n"


def test_merge_is_a_no_op_when_the_owner_already_set_everything():
    existing = "[wsl2]\nMemory=4GB\nprocessors=2\n"
    plan = merge_wslconfig(existing, {"memory": "8192MB", "processors": "8"})
    assert not plan.changed and plan.text == existing and plan.kept_owner_values == {"memory": "4GB", "processors": "2"}
