"""Host capability flags (cross-platform plan W1, X4 and X5): sanitise what an agent reports and gate placement on it.

An agent's heartbeat ``health`` block carries a ``platform`` object and a ``capabilities`` list. Both are SELF-REPORTED:
they can only narrow where work is placed, never widen what a tier proves (sealed, sealed_pro and confidential still go
through attestation in ``scheduler._machine_supports_tier_and_policy``).

Rules (all fail closed, L1):

* Unknown flags are dropped; at most ``len(KNOWN_FLAGS)`` flags are kept; platform strings are short ASCII.
* A host that sends a ``platform`` object is a "flag host". It must list every flag its jobs need. ``capabilities``
  absent or empty on a flag host refuses every job (even a plain container job needs ``container_tier``).
* A host with NO ``platform`` object is a legacy Linux agent from before this plan. It keeps working exactly as it does
  today: no flag is required of it here and the existing gates (isolation runtimes, vfio_gpu, cpu_capable, tiers) decide.
* X5: a host whose ``platform.os`` is ``windows`` NEVER receives private-tier (sealed, sealed_pro, confidential), BYOC,
  gVisor, microVM, Windows-VM-tier or multi-node work, whatever flags it claims.

This module is pure (no database, no settings) so every placement path can call it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

KNOWN_FLAGS: tuple[str, ...] = (
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
_KNOWN = frozenset(KNOWN_FLAGS)
MAX_FLAGS = len(KNOWN_FLAGS)
MAX_INPUT_ITEMS = 64  # scan at most this many list items from an untrusted heartbeat

KNOWN_OS = ("linux", "windows")
PLATFORM_KEYS = ("os", "os_version", "runtime_backend", "agent_version", "supervisor_version")
MAX_PLATFORM_VALUE_LEN = 64
_PLATFORM_VALUE_RE = re.compile(r"^[A-Za-z0-9._+\-: ]{1,64}$")

# Flags a Windows host is never granted (X5), even when it claims them.
# amd_rocm: AMD on Windows is a default no-go (docs/architecture/W7-amd-on-windows-decision.md).
WINDOWS_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {"gvisor", "microvm", "sealed", "sealed_pro", "confidential", "vfio_gpu", "multi_node", "amd_rocm"}
)
PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")


# --------------------------------------------------------------------------- sanitising (heartbeat input)
def sanitize_capabilities(value: Any) -> list[str] | None:
    """Known flags only, de-duplicated, in ``KNOWN_FLAGS`` order. None stays None (absent); anything that is not a list
    becomes ``[]`` (present but proving nothing). Never raises."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return []
    seen: set[str] = set()
    for item in list(value)[:MAX_INPUT_ITEMS]:
        if isinstance(item, str) and item in _KNOWN:
            seen.add(item)
    return [f for f in KNOWN_FLAGS if f in seen]


def sanitize_platform(value: Any) -> dict[str, str] | None:
    """The ``platform`` object reduced to the known keys with short ASCII string values. ``os`` must be a known OS or it
    is dropped. None stays None (a legacy agent); a non-dict becomes ``{}`` (a flag host that told us nothing). Never raises."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key in PLATFORM_KEYS:
        raw = value.get(key)
        if not isinstance(raw, str):
            continue
        raw = raw.strip()
        if not _PLATFORM_VALUE_RE.fullmatch(raw):
            continue
        if key == "os":
            raw = raw.lower()
            if raw not in KNOWN_OS:
                continue
        out[key] = raw
    return out


# --------------------------------------------------------------------------- what a machine reports
@dataclass(frozen=True)
class HostCapabilities:
    legacy: bool  # no platform object: a pre-W1 Linux agent
    os: str | None  # "linux" | "windows" | None (unknown)
    flags: frozenset[str]
    has_capabilities: bool  # the heartbeat carried a (possibly empty) capabilities list

    @property
    def is_windows(self) -> bool:
        return self.os == "windows"


def host_capabilities(machine: Any) -> HostCapabilities:
    """Read a machine's stored heartbeat health. Re-sanitises on read: rows written by older code or by other paths are
    never trusted."""
    health = getattr(machine, "last_health", None)
    if not isinstance(health, dict):
        return HostCapabilities(legacy=True, os=None, flags=frozenset(), has_capabilities=False)
    platform = sanitize_platform(health.get("platform"))
    caps = sanitize_capabilities(health.get("capabilities"))
    if platform is None:
        # No platform object: a legacy agent. Any capabilities it sent without one are ignored (not a W1 agent).
        return HostCapabilities(legacy=True, os=None, flags=frozenset(), has_capabilities=False)
    flags = frozenset(caps or [])
    os_name = platform.get("os")
    if os_name == "windows":
        flags = flags - WINDOWS_FORBIDDEN_FLAGS
    return HostCapabilities(legacy=False, os=os_name, flags=flags, has_capabilities=bool(caps))


HOST_KINDS = ("dedicated", "shared")


def machine_host_kind(machine: Any) -> str:
    """``"dedicated"``, ``"shared"`` or ``"unspecified"``.

    SELF-DECLARED by the host (``host_kind`` in the heartbeat health block): ``dedicated`` means the machine does nothing
    but run ComputeCoin jobs (a server), ``shared`` means a person also uses it (a laptop or gaming PC). A Windows host is
    ALWAYS ``shared`` whatever it says: it exists to be a PC its owner also uses. A host that never declared one is
    ``unspecified`` and is not treated as dedicated.
    """
    health = getattr(machine, "last_health", None)
    if not isinstance(health, dict):
        return "unspecified"
    platform = sanitize_platform(health.get("platform"))
    if platform is not None and platform.get("os") == "windows":
        return "shared"
    kind = health.get("host_kind")
    return kind if isinstance(kind, str) and kind in HOST_KINDS else "unspecified"


_ACTIVE_JOB_STATES = ("queued", "reserved", "running")


def job_gpu_shared(metrics: Any) -> bool:
    """True only if the host's agent MEASURED another program on the same GPU while the job ran (``metrics.gpu_shared``).

    Absence means "not seen", never "alone on the GPU": an agent that cannot look (an AMD card, a runtime that cannot see
    the owner's programs) reports nothing. Only a real boolean ``True`` counts."""
    return isinstance(metrics, dict) and metrics.get("gpu_shared") is True


def job_gpu_other_processes(metrics: Any) -> int | None:
    """How many other programs were seen on the GPU at the most (0..64), or None when it was not reported."""
    if not job_gpu_shared(metrics):
        return None
    n = metrics.get("gpu_other_processes")
    return max(0, min(int(n), 64)) if isinstance(n, int) and not isinstance(n, bool) else None


def job_risk_note(host_kind: str, job_status: str, attempts_made: int, gpu_shared: bool = False) -> str | None:
    """A plain-language note for the customer about the host their job is on, or None when there is nothing to say.

    Honest about what is known: ``host_kind`` is declared by the host, and ``gpu_shared`` is what the agent measured.
    Never says the job WILL be interrupted."""
    notes: list[str] = []
    if attempts_made > 0:
        notes.append(
            "This job was re-run on another host after the previous host stopped it. You are not charged for the "
            "earlier attempt."
        )
    if gpu_shared:
        notes.append(
            "While your job ran, another program on the host's computer was also using the same graphics card, so the "
            "job may have run slower than usual."
        )
    if host_kind == "shared" and job_status in _ACTIVE_JOB_STATES:
        notes.append(
            "This job is on a shared PC: its owner also uses it, so the job may run slower or be moved to another host "
            "if the owner needs the graphics card. You are never charged for an interrupted attempt. For important "
            "work, choose a dedicated host."
        )
    return " ".join(notes) or None


def job_requires_dedicated(job: Any) -> bool:
    """The customer asked for a dedicated host (``data_policy.require_dedicated_host``)."""
    policy = getattr(job, "data_policy", None)
    return isinstance(policy, dict) and policy.get("require_dedicated_host") is True


def machine_platform_view(machine: Any) -> tuple[dict[str, str] | None, list[str] | None]:
    """(platform, capabilities) for admin output: sanitised, None when the agent never sent them."""
    health = getattr(machine, "last_health", None)
    if not isinstance(health, dict):
        return None, None
    platform = sanitize_platform(health.get("platform"))
    caps = sanitize_capabilities(health.get("capabilities")) if platform is not None else None
    if platform is not None and platform.get("os") == "windows" and caps is not None:
        caps = [c for c in caps if c not in WINDOWS_FORBIDDEN_FLAGS]
    return platform, caps


# --------------------------------------------------------------------------- what a job needs
def _spec_v2(job: Any) -> dict:
    spec = getattr(job, "spec_v2", None)
    return spec if isinstance(spec, dict) else {}


def _inner_spec(job: Any) -> dict:
    inner = _spec_v2(job).get("spec")
    return inner if isinstance(inner, dict) else {}


def _effective_resources(job: Any) -> dict:
    res = _spec_v2(job).get("effective_resources")
    return res if isinstance(res, dict) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def job_is_cpu_only(job: Any) -> bool:
    res = _effective_resources(job)
    return bool(_spec_v2(job)) and "gpu_count" in res and _int(res.get("gpu_count"), -1) == 0


def job_needs_private_tier(job: Any) -> bool:
    return (getattr(job, "isolation_tier", None) or "container").lower() in PRIVATE_TIERS


def job_is_byoc(job: Any) -> bool:
    return _spec_v2(job).get("byoc") is not None


def job_is_windows_vm(job: Any) -> bool:
    inner = _inner_spec(job)
    return inner.get("mode") == "vm" or inner.get("os") == "windows"


def job_node_count(job: Any) -> int:
    return max(1, _int(_effective_resources(job).get("node_count"), 1))


def x5_violation(job: Any) -> str | None:
    """What a Windows host must never be given, or None. These are kinds of work, not flags."""
    tier = (getattr(job, "isolation_tier", None) or "container").lower()
    if tier in PRIVATE_TIERS:
        return "private_tier"
    if tier in ("gvisor", "microvm"):
        return "isolation_runtime"
    if job_is_byoc(job):
        return "byoc"
    if job_is_windows_vm(job):
        return "vm_tier"
    if job_node_count(job) > 1:
        return "multi_node"
    return None


def job_required_flags(job: Any) -> list[tuple[str, ...]]:
    """What the job needs from the host, as a list of any-of groups: each inner tuple is satisfied when the host has AT
    LEAST ONE of its flags. GPU runtime flags are handled by :func:`gpu_flag_reason` (they depend on the runtime)."""
    need: list[tuple[str, ...]] = [("container_tier",)]
    tier = (getattr(job, "isolation_tier", None) or "container").lower()
    if tier == "gvisor":
        need.append(("gvisor",))
    elif tier == "microvm":
        need.append(("microvm",))
    elif tier == "sealed":
        need.append(("sealed", "sealed_pro"))
    elif tier == "sealed_pro":
        need.append(("sealed_pro",))
    elif tier == "confidential":
        need.append(("confidential",))
    byoc = _spec_v2(job).get("byoc")
    if isinstance(byoc, dict) and byoc.get("isolation") in ("gvisor", "microvm"):
        need.append((byoc["isolation"],))
    if getattr(job, "network_policy", None):
        need.append(("egress_proxy",))
    if job_is_cpu_only(job):
        need.append(("cpu_capable",))
    if job_is_windows_vm(job):
        need.append(("vfio_gpu",))
    if job_node_count(job) > 1:
        need.append(("multi_node",))
    return need


# --------------------------------------------------------------------------- the gates
def capability_refusal(job: Any, machine: Any) -> str | None:
    """Why ``machine`` may not receive ``job`` on capability grounds, or None.

    Reason codes: ``host_not_dedicated``, ``windows_host_<kind>`` (X5) and ``capability_missing_<flag>[_or_<flag>]``.
    """
    host = host_capabilities(machine)
    if job_requires_dedicated(job) and machine_host_kind(machine) != "dedicated":
        return "host_not_dedicated"  # the customer wants a machine nobody else is using; only a declared dedicated host
    if host.is_windows:
        kind = x5_violation(job)
        if kind:
            return f"windows_host_{kind}"
    if host.legacy:
        return None  # pre-W1 Linux agent: unchanged, the existing gates decide
    if not host.has_capabilities:
        return "capability_missing_container_tier"  # a flag host that proved nothing gets nothing (fail closed)
    for group in job_required_flags(job):
        if not any(flag in host.flags for flag in group):
            return "capability_missing_" + "_or_".join(group)
    return None


def gpu_flag_reason(machine: Any, runtime: str | None, gpu_count: int) -> str | None:
    """Capability gate for GPU work: the runtime flag (``nvidia_cuda`` / ``amd_rocm``) and, for 2+ GPUs, ``multi_gpu``.
    ``runtime`` is the machine's runtime (what the job will run on). None for legacy hosts and for CPU work."""
    if gpu_count <= 0:
        return None
    host = host_capabilities(machine)
    if host.legacy:
        return None
    if not host.has_capabilities:
        return "capability_missing_container_tier"
    rt = (runtime or "cuda").lower().strip()
    flag = "amd_rocm" if rt == "rocm" else "nvidia_cuda"
    if flag not in host.flags:
        return f"capability_missing_{flag}"
    if gpu_count >= 2 and "multi_gpu" not in host.flags:
        return "capability_missing_multi_gpu"
    return None
