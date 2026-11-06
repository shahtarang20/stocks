"""Configuration file content for the runtime distro, and the non-clobbering ``.wslconfig`` merge.

``/etc/wsl.conf`` (X6): automount off, interop off, Windows PATH not appended, systemd on, default user = the runner user.
``provider-agent/windows/wsl.conf.template`` is the same text with ``{{runner_user}}`` in place of the user name; a test
keeps the two identical.

RESOURCE LIMITS (cross-platform plan section 4.2: "document that WSL2 limits are per user")
-------------------------------------------------------------------------------------------
``%UserProfile%\\.wslconfig`` is ONE file per Windows user and its ``[wsl2]`` keys (memory, processors, swap, ...) configure
the single utility VM shared by EVERY WSL2 distro of that user, including the owner's own Ubuntu. There is no per-distro
memory or CPU key. The module therefore:

* never writes ``.wslconfig`` unless ``wslconfig_policy`` is ``"add_missing"`` (default ``"never"``);
* with ``add_missing`` it only ADDS keys the owner has not set, each preceded by a marker comment, never edits or removes
  an owner line, keeps a one-time backup (``.wslconfig.computecoin.bak``) and can remove exactly what it added;
* never runs ``wsl --shutdown`` (that would stop the owner's other distros); a changed ``.wslconfig`` only takes effect
  after the owner shuts WSL down, which the result reports as ``restart_required``;
* gets its real per-distro caps elsewhere: a systemd slice (``computecoin.slice``) with ``MemoryMax`` / ``CPUQuota``
  that Docker uses as ``cgroup-parent`` for every job container, and a virtual-disk cap through
  ``wsl --manage computecoin --resize``.

TODO(verify-docs) (X11): ``.wslconfig`` and ``wsl.conf`` keys and sections
(https://learn.microsoft.com/windows/wsl/wsl-config), systemd cgroup v2 controllers available in the WSL2 kernel,
Docker ``cgroup-parent`` with the systemd cgroup driver, and the daemon.json keys below.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .distro_paths import RUNNER_USER, SLICE_NAME

WSL_CONF_TEMPLATE = """# Managed by ComputeCoin. Edits are overwritten by repair. Windows files must never be reachable from jobs (X6).
[automount]
enabled=false
mountFsTab=false

[interop]
enabled=false
appendWindowsPath=false

[boot]
systemd=true

[user]
default={{runner_user}}
"""

REQUIRED_HARDENING = (
    ("automount", "enabled", "false"),
    ("interop", "enabled", "false"),
    ("interop", "appendwindowspath", "false"),
    ("boot", "systemd", "true"),
)


def render_wsl_conf(runner_user: str = RUNNER_USER) -> str:
    return WSL_CONF_TEMPLATE.replace("{{runner_user}}", runner_user)


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """Case-insensitive section and key names, ``#`` and ``;`` comments, ``key=value`` lines."""
    out: dict[str, dict[str, str]] = {}
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            out.setdefault(section, {})
            continue
        key, sep, value = line.partition("=")
        if sep:
            out.setdefault(section, {})[key.strip().lower()] = value.strip().lower()
    return out


def hardening_problems(wsl_conf_text: Optional[str]) -> list[str]:
    """What is wrong with the content of /etc/wsl.conf (empty list = every X6 key is as required)."""
    if wsl_conf_text is None:
        return ["/etc/wsl.conf is missing"]
    ini = parse_ini(wsl_conf_text)
    problems = []
    for section, key, want in REQUIRED_HARDENING:
        have = ini.get(section, {}).get(key)
        if have != want:
            problems.append(f"[{section}] {key} is {have!r}, must be {want!r}")
    return problems


# --- per-distro limits ----------------------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResourceLimits:
    """Supplied by the supervisor (``set_limits``). ``None`` = no cap of that kind (the owner's VM-wide limits still apply)."""

    memory_mb: Optional[int] = None  # cap for ALL job containers together (systemd MemoryMax of the slice)
    cpus: Optional[float] = None  # CPUQuota of the slice = cpus * 100 %
    disk_gb: Optional[int] = None  # virtual disk size cap of the distro (wsl --manage --resize)
    vm_memory_mb: Optional[int] = None  # only used when wslconfig_policy == "add_missing"
    vm_processors: Optional[int] = None  # only used when wslconfig_policy == "add_missing"

    def validate(self) -> None:
        if self.memory_mb is not None and self.memory_mb < 512:
            raise ValueError("memory_mb must be at least 512")
        if self.cpus is not None and not (0.25 <= self.cpus <= 1024):
            raise ValueError("cpus must be between 0.25 and 1024")
        if self.disk_gb is not None and self.disk_gb < 20:
            raise ValueError("disk_gb must be at least 20")
        if self.vm_memory_mb is not None and self.vm_memory_mb < 1024:
            raise ValueError("vm_memory_mb must be at least 1024")
        if self.vm_processors is not None and self.vm_processors < 1:
            raise ValueError("vm_processors must be at least 1")


def render_slice(limits: ResourceLimits) -> str:
    limits.validate()
    lines = [
        "# Managed by ComputeCoin. Docker uses this slice as cgroup-parent for every job container.",
        "[Unit]", "Description=ComputeCoin job containers (resource cap)", "",
        "[Slice]", "MemoryAccounting=yes", "CPUAccounting=yes", "TasksAccounting=yes",
    ]
    if limits.memory_mb is not None:
        lines += [f"MemoryMax={int(limits.memory_mb)}M", "MemorySwapMax=0"]
    if limits.cpus is not None:
        lines.append(f"CPUQuota={int(round(limits.cpus * 100))}%")
    return "\n".join(lines) + "\n"


def disk_size_arg(limits: ResourceLimits) -> Optional[str]:
    return None if limits.disk_gb is None else f"{int(limits.disk_gb)}GB"


def render_daemon_json() -> str:
    """Docker daemon settings. No TCP listener is configured (the socket stays local). ``nvidia-ctk runtime configure`` adds the
    nvidia runtime to this file afterwards."""
    return json.dumps({
        "cgroup-parent": SLICE_NAME,
        "no-new-privileges": True,
        "userland-proxy": False,
        "log-driver": "local",
        "log-opts": {"max-size": "20m", "max-file": "3"},
    }, indent=2, sort_keys=True) + "\n"


# --- .wslconfig merge (never clobber the owner) ---------------------------------------------------------------- #
MARKER = "# computecoin-managed: added by ComputeCoin; delete this line and the next to undo"
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")


@dataclass
class WslConfigPlan:
    text: str
    added: dict[str, str] = field(default_factory=dict)
    kept_owner_values: dict[str, str] = field(default_factory=dict)  # keys the owner already set: left alone

    @property
    def changed(self) -> bool:
        return bool(self.added)


def desired_wslconfig(limits: ResourceLimits) -> dict[str, str]:
    out: dict[str, str] = {}
    if limits.vm_memory_mb is not None:
        out["memory"] = f"{int(limits.vm_memory_mb)}MB"
    if limits.vm_processors is not None:
        out["processors"] = str(int(limits.vm_processors))
    return out


def merge_wslconfig(existing: str, desired: dict[str, str]) -> WslConfigPlan:
    """Add missing ``[wsl2]`` keys. Existing lines are copied byte for byte; nothing the owner wrote is changed or removed."""
    lines = existing.splitlines()
    eol = "\r\n" if "\r\n" in existing else "\n"
    start = end = None  # [wsl2] section: lines[start] is the header, lines[start+1:end] its body
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m and start is not None and end is None:
            end = i
        if m and m.group(1).strip().lower() == "wsl2" and start is None:
            start = i
    if start is not None and end is None:
        end = len(lines)
    present: dict[str, str] = {}
    if start is not None:
        for line in lines[start + 1:end]:
            km = _KEY.match(line)
            if km:
                present[km.group(1).lower()] = line.split("=", 1)[1].strip()
    plan = WslConfigPlan(text=existing)
    missing = {}
    for key, value in desired.items():
        if key.lower() in present:
            plan.kept_owner_values[key] = present[key.lower()]
        else:
            missing[key] = value
    if not missing:
        return plan
    block: list[str] = []
    for key, value in missing.items():
        block += [MARKER, f"{key}={value}"]
    if start is None:
        new = list(lines)
        if new and new[-1].strip():
            new.append("")
        new += ["[wsl2]", *block]
    else:
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1  # keep trailing blank lines after our block, next to the following section
        new = lines[:insert_at] + block + lines[insert_at:]
    plan.text = eol.join(new) + eol
    plan.added = dict(missing)
    return plan


def remove_managed(existing: str) -> str:
    """Undo ``merge_wslconfig``: drop each marker line and the single key line after it. Owner lines are untouched."""
    lines = existing.splitlines()
    eol = "\r\n" if "\r\n" in existing else "\n"
    out: list[str] = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == MARKER:
            skip_next = True
            continue
        out.append(line)
    return (eol.join(out) + eol) if out else ""
