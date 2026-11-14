"""Linux readers for "step aside when the owner is busy" (decision X13) and for "is this GPU shared right now".

The rules engine (``windows/governance/policy.py``) is pure Python and takes injected probes; Windows has Win32 probes and
until now Linux had none. These are the Linux ones:

* :class:`LinuxActivityProbe`  how long the person has been idle, from systemd-logind (``loginctl``);
* battery state                 from ``platform.linux.LinuxPower`` (host paths live in the platform layer);
* :class:`NvidiaGpuUsers`      other programs using the GPU, and the free GPU memory, from ``nvidia-smi``.

Every reader answers ``None`` ("I cannot tell") rather than a guess, and never raises: the engine treats an unknown signal
conservatively (see ``UNKNOWN_BLOCKS_DEFAULT``), so a probe that fails can only make the machine MORE careful.

What is NOT known on Linux, and why this matters to a host:

* full-screen apps (no portable way without an X11 or Wayland client): reported unknown and not used to block;
* a pending reboot (Ubuntu's ``reboot-required`` file lingers for days): not used;
* GPU use is read for NVIDIA cards only. An AMD card reports nothing, which means "none seen", never "free for sure".

None of this was run against a real desktop, a real battery or a real ``nvidia-smi`` here: the parsers are tested against
sample output written from the tools' documented formats.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .platform import ActivitySnapshot

#: Desktop plumbing that uses the GPU all the time and says nothing about the owner playing or working.
LINUX_IGNORED_PROCESSES = frozenset({
    "xorg", "x", "xwayland", "gnome-shell", "gnome-shell-calendar-server", "mutter", "kwin_x11", "kwin_wayland", "kwin",
    "plasmashell", "sway", "weston", "wayfire", "hyprland", "cinnamon", "marco", "mate-panel", "xfwm4", "xfce4-panel",
    "picom", "compton", "gdm", "gdm-x-session", "gdm-wayland-session", "sddm", "lightdm", "budgie-wm", "nvidia-persistenced",
})

# A process whose cgroup path mentions one of these runs in a container: it is a job (ours, or someone's), not the owner.
_CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "libpod", "crio", "kubepods", "lxc")

Runner = Callable[[list], Optional[str]]


def _run(argv: list, timeout: float = 5.0) -> Optional[str]:
    """Run a command and return its stdout, or None if it is missing, fails or hangs. Never raises."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


# --------------------------------------------------------------------------- #
# GPU: who else is using it, and how much memory is free
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GpuProcess:
    pid: int
    kind: str  # "C" compute, "G" graphics, "C+G" both (as nvidia-smi pmon prints it)
    command: str


def parse_pmon(text: str) -> list:
    """Parse ``nvidia-smi pmon -c 1``. Comment lines start with ``#``; each process line is
    ``gpu pid type sm mem enc dec ... command``. Columns after ``type`` differ between driver versions, so only the
    GPU index, pid, type and the LAST token (the command) are read. A line that does not fit is skipped."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            int(parts[0])
            pid = int(parts[1])
        except ValueError:
            continue
        out.append(GpuProcess(pid=pid, kind=parts[2], command=parts[-1]))
    return out


def _in_container(pid: int, proc_root: str) -> Optional[bool]:
    """True when the process runs in a container, False when it does not, None when that cannot be read."""
    try:
        with open(os.path.join(proc_root, str(pid), "cgroup"), encoding="utf-8", errors="replace") as fh:
            text = fh.read().lower()
    except OSError:
        return None
    return any(marker in text for marker in _CONTAINER_CGROUP_MARKERS)


def _basename(command: str) -> str:
    return os.path.basename(command.strip()).lower()


class NvidiaGpuUsers:
    """Other programs on the GPU (``nvidia-smi``). A job's own container is never counted as the owner."""

    def __init__(
        self,
        run: Optional[Runner] = None,
        *,
        proc_root: str = "/proc",
        ignored: frozenset = LINUX_IGNORED_PROCESSES,
    ) -> None:
        self._run = run or _run
        self._proc_root = proc_root
        self._ignored = frozenset(str(n).lower() for n in ignored)

    def other_processes(self) -> Optional[list]:
        """Names (lower case, no path) of GPU programs that are neither desktop plumbing nor a container.

        ``[]`` means "looked, nobody else is on the GPU". ``None`` means "could not look" (no NVIDIA driver, no
        ``nvidia-smi``, a failure): the caller must not read that as free."""
        text = self._run(["nvidia-smi", "pmon", "-c", "1"])
        if text is None:
            return None
        names = []
        for proc in parse_pmon(text):
            name = _basename(proc.command)
            if not name or name in self._ignored:
                continue
            if _in_container(proc.pid, self._proc_root) is True:
                continue
            names.append(name)
        return names

    def foreground_gpu_app(self) -> Optional[str]:
        """The engine's probe name: the first other GPU program, or None when none was seen or it could not be read."""
        names = self.other_processes()
        return names[0] if names else None

    def free_vram_mb(self) -> Optional[int]:
        """GPU memory free on the card with the most free memory, or None when it cannot be read."""
        text = self._run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
        if text is None:
            return None
        values = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                values.append(int(float(line)))
            except ValueError:
                return None  # an odd answer is not a number to act on
        return max(values) if values else None


# --------------------------------------------------------------------------- #
# Owner at the keyboard? (systemd-logind)
# --------------------------------------------------------------------------- #
def parse_logind_idle(text: str, now: float) -> ActivitySnapshot:
    """From ``loginctl show-session`` output: ``IdleHint=yes|no`` and ``IdleSinceHint=<microseconds since the epoch>``.

    ``IdleHint=no``: somebody used the machine recently (idle 0 seconds). ``yes``: idle since the given time. Anything
    else is unknown."""
    props = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    hint = props.get("IdleHint", "").lower()
    if hint == "no":
        return ActivitySnapshot(owner_active=True, idle_seconds=0.0)
    if hint == "yes":
        try:
            since_us = int(props.get("IdleSinceHint", "0"))
        except ValueError:
            return ActivitySnapshot()
        if since_us <= 0:
            return ActivitySnapshot()
        return ActivitySnapshot(owner_active=False, idle_seconds=max(0.0, now - since_us / 1_000_000.0))
    return ActivitySnapshot()


class LinuxActivityProbe:
    def __init__(self, run: Optional[Runner] = None, *, clock: Callable[[], float] = time.time,
                 session_id: Optional[str] = None) -> None:
        self._run = run or _run
        self._clock = clock
        self._session = session_id or os.environ.get("XDG_SESSION_ID") or "auto"

    def snapshot(self) -> ActivitySnapshot:
        text = self._run(["loginctl", "show-session", self._session, "--property=IdleHint", "--property=IdleSinceHint"])
        if text is None:
            return ActivitySnapshot()  # no logind, no graphical session (a server): unknown
        return parse_logind_idle(text, self._clock())
