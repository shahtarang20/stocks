"""Capability advertisement (cross-platform plan W1, X4): what this host can PROVE it supports.

The heartbeat ``health`` block carries a ``platform`` object and a ``capabilities`` list of flags. The scheduler only
places work that needs a flag on a host that reports it. A flag is added only when a probe says so; nothing is a default.

Known flags (plan section 3.4): container_tier, nvidia_cuda, amd_rocm, gvisor, microvm, sealed, sealed_pro,
confidential, vfio_gpu, multi_node, egress_proxy, thermal_cap, power_limit, multi_gpu, cpu_capable.

A Windows host reports ONLY the container-tier flags (X5): gvisor, microvm, sealed*, confidential, vfio_gpu and
multi_node are never emitted on Windows even if a probe claimed them. The server enforces the same rule independently
(apps/api/app/services/capability_flags.py) because an agent's claim is not evidence.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .platform import PLATFORM_WINDOWS, Platform, get_platform

KNOWN_CAPABILITY_FLAGS: tuple[str, ...] = (
    "container_tier",
    "nvidia_cuda",
    "amd_rocm",
    "gvisor",
    "microvm",
    "sealed",
    "sealed_pro",
    "confidential",
    "vfio_gpu",
    "multi_node",
    "egress_proxy",
    "thermal_cap",
    "power_limit",
    "multi_gpu",
    "cpu_capable",
)

# Never advertised by a Windows host (X5): isolation beyond a container, privacy tiers, VM tier, multi-node.
# amd_rocm: AMD on Windows is a default no-go (docs/architecture/W7-amd-on-windows-decision.md).
WINDOWS_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {"gvisor", "microvm", "sealed", "sealed_pro", "confidential", "vfio_gpu", "multi_node", "amd_rocm"}
)

_PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")


def _load_config() -> Any:
    try:
        from .config import AgentConfig

        return AgentConfig.load()
    except Exception:
        return None


def compute_capabilities(
    report: Any,
    *,
    config: Any = None,
    plat: Optional[Platform] = None,
    isolation_runtimes_fn: Optional[Callable[[], list[str]]] = None,
    vfio_fn: Optional[Callable[[], Optional[bool]]] = None,
) -> list[str]:
    """The flags this host reports, in the stable ``KNOWN_CAPABILITY_FLAGS`` order.

    ``report`` is the :class:`~computecoin_agent.hardware.CapabilityReport`. ``config`` defaults to the loaded agent
    config (a failed load means no operator opt-ins). Every probe failure means "flag absent" (L1).
    """
    plat = plat or get_platform()
    cfg = config if config is not None else _load_config()
    windows = plat.name == PLATFORM_WINDOWS
    flags: set[str] = set()

    try:
        container = bool(plat.runtime.engine_available())
    except Exception:
        container = False
    if container:
        flags.add("container_tier")

    gpus = list(getattr(report, "gpus", None) or [])
    nvidia = [g for g in gpus if getattr(g, "gpu_vendor", "nvidia") == "nvidia" and getattr(g, "gpu_runtime", "cuda") == "cuda"]
    amd = [g for g in gpus if getattr(g, "gpu_vendor", "") == "amd"]
    if container and nvidia:
        flags.add("nvidia_cuda")
    try:
        kfd = bool(plat.hardware.amd_kfd_present())
    except Exception:
        kfd = False
    if container and amd and kfd:
        flags.add("amd_rocm")
    if container and (len(nvidia) >= 2 or len(amd) >= 2):
        flags.add("multi_gpu")
    if gpus:
        flags.add("thermal_cap")  # the thermal guard reads NVML / amdsmi telemetry, which exists exactly when a GPU is detected

    limit_w = getattr(cfg, "gpu_power_limit_w", None)
    if limit_w is not None and limit_w > 0:
        try:
            if plat.gpu.power_limit_supported():
                flags.add("power_limit")
        except Exception:
            pass
    if container and getattr(cfg, "egress_proxy_image", None):
        flags.add("egress_proxy")
    if container and getattr(cfg, "cpu_only_kits_enabled", False):
        flags.add("cpu_capable")

    if not windows:
        runtimes = (isolation_runtimes_fn or _isolation_runtimes)()
        if "gvisor" in runtimes:
            flags.add("gvisor")
        if "microvm" in runtimes:
            flags.add("microvm")
        for tier in getattr(report, "isolation_tiers", None) or []:
            if tier in _PRIVATE_TIERS:
                flags.add(tier)
        if (vfio_fn or _vfio_gpu)() is True:
            flags.add("vfio_gpu")
        if getattr(cfg, "multinode_network", None) and getattr(cfg, "multinode_network_firewalled", False) and container:
            flags.add("multi_node")

    flags -= WINDOWS_FORBIDDEN_FLAGS if windows else set()
    return [f for f in KNOWN_CAPABILITY_FLAGS if f in flags]


def _isolation_runtimes() -> list[str]:
    from .heartbeat import isolation_runtimes

    return isolation_runtimes()


def _vfio_gpu() -> Optional[bool]:
    from .heartbeat import _vfio_gpu_flag

    return _vfio_gpu_flag()
