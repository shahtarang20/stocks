"""W7 Windows GPU features.

* Thermal cap and hard stop: NATIVE NVML on the Windows side (not inside the distro: WSL2 exposes no sensors to the guest).
  ``thermal.ThermalGuard`` is reused unchanged; only its sampler is replaced by :class:`NvmlProbe`.
* Power limit: ``nvidia-smi -pl`` on the Windows side. It needs an elevated (administrator) process, it is best effort
  (it NEVER fails enrolment) and it is not persistent: the driver resets it at reboot or driver reload, so the supervisor
  re-applies it on every start. Bounds come from ``nvidia-smi --query-gpu=power.min_limit,power.max_limit``; a value outside
  them is refused rather than clamped.
* Multi-GPU: the device set is chosen by the unchanged ``gpu_alloc.GpuAllocator`` inside the runner. What is new is the
  CHECK that ``docker run --gpus '"device=N"'`` really selects one card under WSL2 (spec in :func:`device_selection_spec`).
* AMD: ``amd_rocm`` is never advertised from a Windows host (docs/architecture/W7-amd-on-windows-decision.md).

TODO(verify-docs) (X11): NVML on Windows (library load path, ``nvmlDeviceGetPowerUsage`` units), ``nvidia-smi -pl`` syntax and
admin requirement on Windows drivers and its behaviour on GeForce cards (many GeForce boards refuse power limit changes),
``--gpus device=`` support and index/UUID semantics under WSL2 (NVIDIA CUDA on WSL user guide, NVIDIA Container Toolkit
docs). None of it was checked or run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from . import distro_paths as P
from .health import Check, parse_gpu_list, smoke_argv
from .wsl import RunResult, WslCli

logger = logging.getLogger(__name__)

# Capability flags (cross-platform plan section 3.4) that a Windows host must never report (X4, X5, W7.3).
FORBIDDEN_ON_WINDOWS = frozenset({
    "amd_rocm", "gvisor", "microvm", "sealed", "sealed_pro", "confidential", "vfio_gpu", "multi_node",
})


# --- native NVML thermal -------------------------------------------------------------------------------------------- #
class NvmlProbe:
    """Samples every NVIDIA GPU through NVML (``nvidia-ml-py``) on the Windows host.

    Differences from ``telemetry.sample_gpu_metrics``: never returns the simulated fallback sample (a made-up 48.5 C
    would hide a real overheating card), and always returns ``[]`` on any failure. ``ThermalGuard`` treats ``[]`` as
    "no reading": it never blocks and never triggers on it (thermal.py contract).
    """

    def __init__(self, nvml_module: Any = None, sampler: Optional[Callable[..., list[Any]]] = None) -> None:
        self._nvml = nvml_module
        self._sampler = sampler

    def _module(self) -> Any:
        if self._nvml is not None:
            return self._nvml
        try:
            import pynvml  # type: ignore  # TODO(verify-docs): loads nvml.dll from the Windows driver
        except Exception:
            return None
        return pynvml

    def sample(self) -> list[Any]:
        nvml = self._module()
        if nvml is None:
            return []
        try:
            nvml.nvmlInit()
        except Exception as exc:
            logger.debug("NVML init failed: %s", exc)
            return []
        try:
            if self._sampler is not None:
                samples = self._sampler(nvml_module=nvml)
            else:
                from ...telemetry import sample_gpu_metrics

                samples = sample_gpu_metrics(nvml_module=nvml, simulated=False)
            return [s for s in (samples or []) if (getattr(s, "extra_metrics", None) or {}).get("mode") != "simulated_probe"]
        except Exception as exc:
            logger.debug("NVML sample failed: %s", exc)
            return []
        finally:
            try:
                nvml.nvmlShutdown()
            except Exception:
                pass


def thermal_guard(config: Any, probe: Optional[NvmlProbe] = None):
    """The existing ThermalGuard (B6 logic: cap, hysteresis, hard stop) fed by the native probe."""
    from ...thermal import ThermalGuard

    return ThermalGuard.from_config(config, sampler=(probe or NvmlProbe()).sample)


# --- power limit (nvidia-smi -pl, needs administrator) -------------------------------------------------------------- #
Exec = Callable[[Sequence[str]], RunResult]


@dataclass
class PowerLimitResult:
    applied: list[int] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.applied) and not self.errors


def _parse_power_bounds(csv_text: str) -> dict[int, tuple[float, float]]:
    out: dict[int, tuple[float, float]] = {}
    for line in csv_text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            out[int(parts[0])] = (float(parts[1]), float(parts[2]))
        except ValueError:
            continue  # "[N/A]" on boards without power management
    return out


def apply_power_limit(limit_w: Optional[float], *, run: Exec, is_admin: Callable[[], bool], nvidia_smi: str = "nvidia-smi") -> PowerLimitResult:
    """Best effort. Never raises, and the caller must never fail enrolment on the result (W7)."""
    res = PowerLimitResult()
    if limit_w is None:
        res.skipped_reason = "no power limit configured"
        return res
    if limit_w <= 0:
        res.skipped_reason = "power limit must be positive"
        return res
    try:
        if not is_admin():
            res.skipped_reason = "needs_admin: nvidia-smi -pl requires an elevated process"
            return res
        q = run([nvidia_smi, "--query-gpu=index,power.min_limit,power.max_limit", "--format=csv,noheader,nounits"])
        bounds = _parse_power_bounds(q.stdout) if q.ok else {}
        if not bounds:
            res.skipped_reason = "power limits are not readable on this GPU or driver"
            return res
        for index, (low, high) in sorted(bounds.items()):
            if not (low <= limit_w <= high):
                res.errors.append(f"gpu {index}: {limit_w} W is outside {low}..{high} W")
                continue
            out = run([nvidia_smi, "-i", str(index), "-pl", str(int(limit_w))])
            if out.ok:
                res.applied.append(index)
            else:
                res.errors.append(f"gpu {index}: {(out.stderr or out.stdout).strip()[:160]}")
    except Exception as exc:  # belt and braces: never propagate
        res.errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
    return res


# --- multi-GPU device sets inside the distro -------------------------------------------------------------------------- #
def discover_gpus(cli: WslCli, image: str, *, runner_user: str = P.RUNNER_USER) -> list[tuple[int, str, str]]:
    """(index, name, uuid) of the GPUs a container sees with ``--gpus all`` (the same view jobs get)."""
    res = cli.exec(smoke_argv(image, ["--gpus", "all"]), user=runner_user, timeout=300)
    return parse_gpu_list(res.stdout) if res.ok else []


def build_allocator(total_gpus: int):
    """The unchanged allocator of the runner (U13); the Windows side only needs it to PLAN device sets for the checks."""
    from ...gpu_alloc import GpuAllocator

    return GpuAllocator(total_gpus)


@dataclass(frozen=True)
class DeviceCase:
    id: str
    device_ids: tuple[int, ...]
    expect_gpu_lines: int
    expect_success: bool


def device_selection_spec(total_gpus: int) -> list[DeviceCase]:
    """Smoke-test SPEC for ``--gpus device=`` under WSL2 (hardware item W7.2).

    D1..Dn: each single index must show exactly one GPU whose UUID equals the UUID that ``--gpus all`` lists at that index.
    P1: indices 0 and 1 together (needs >= 2 GPUs, uses the quoted ``"device=0,1"`` form of gpu_alloc) show exactly two.
    N1: an index past the last GPU must FAIL (no silent fallback to all GPUs).
    """
    cases = [DeviceCase(f"D{i}", (i,), 1, True) for i in range(total_gpus)]
    if total_gpus >= 2:
        cases.append(DeviceCase("P1", (0, 1), 2, True))
    cases.append(DeviceCase("N1", (total_gpus,), 0, False))
    return cases


def verify_device_selection(cli: WslCli, image: str, *, runner_user: str = P.RUNNER_USER) -> list[Check]:
    from ...gpu_alloc import nvidia_gpus_args

    all_gpus = discover_gpus(cli, image, runner_user=runner_user)
    if not all_gpus:
        return [Check("device_selection", False, "no GPU visible with --gpus all")]
    uuid_at = {i: u for i, _n, u in all_gpus}
    checks: list[Check] = []
    for case in device_selection_spec(len(all_gpus)):
        try:
            res = cli.exec(smoke_argv(image, nvidia_gpus_args(list(case.device_ids))), user=runner_user, timeout=300)
            seen = parse_gpu_list(res.stdout)
            if not case.expect_success:
                ok = not res.ok
                detail = "refused as required" if ok else f"index {case.device_ids[0]} did not fail ({len(seen)} GPU line(s))"
            else:
                want = {uuid_at.get(i) for i in case.device_ids}
                ok = res.ok and len(seen) == case.expect_gpu_lines and {u for _i, _n, u in seen} == want
                detail = f"{len(seen)} GPU line(s)" + ("" if ok else f"; wanted {case.expect_gpu_lines} matching {sorted(map(str, want))}")
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
        checks.append(Check(f"device_selection_{case.id}", ok, detail))
    return checks


# --- capabilities ------------------------------------------------------------------------------------------------------- #
def runtime_capabilities(*, smoke_passed: bool, egress_verified: bool, thermal_readable: bool, power_limit_applied: bool,
                         device_selection_verified: bool, gpu_count: int) -> list[str]:
    """Capability flags this runtime can PROVE (X4). Every flag needs its own passed check; none is ever defaulted on."""
    caps: list[str] = []
    if smoke_passed:
        caps += ["container_tier", "nvidia_cuda"]
        if egress_verified:
            caps.append("egress_proxy")
        if thermal_readable:
            caps.append("thermal_cap")
        if power_limit_applied:
            caps.append("power_limit")
        if device_selection_verified and gpu_count > 1:
            caps.append("multi_gpu")
    leaked = FORBIDDEN_ON_WINDOWS.intersection(caps)
    if leaked:  # cannot happen by construction; the guard documents and enforces the rule
        raise AssertionError(f"forbidden capability on Windows: {sorted(leaked)}")
    return caps
