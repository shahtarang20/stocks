"""Platform Abstraction Layer (PAL, cross-platform plan X8).

Every OS-specific concern of the provider agent sits behind one interface in this package. Code OUTSIDE this
package must not know which OS it runs on: it must not call ``os.getuid``, ``os.mkfifo``, ``os.chmod``,
``signal.signal``, read ``/proc``, ``/sys`` or ``/dev``, or assume a path separator. The lint test
``tests/test_platform_boundary.py`` enforces this.

Layout
------
* ``Paths``            where host files live; owner-only modes; container-visible modes
* ``Secrets``          store and read a credential file (0600 file on Linux, DPAPI on Windows)
* ``ServiceControl``   systemd / Windows Service control manager
* ``SignalsShutdown``  SIGTERM / console-control / service-stop handling
* ``FifoOrPipe``       the private job stream (FIFO on Linux)
* ``OwnerIdentity``    uid / admin status of the process owner
* ``RamWorkdir``       RAM-only work directories (tmpfs verification)
* ``HardwareProbe``    memory, CPU and device probes
* ``GpuControl``       GPU thermal / power control surface
* ``PowerState``       sleep, reboot-pending, battery
* ``ActivityProbe``    is the machine's owner using it right now
* ``RuntimeBackend``   how the Linux runner and Docker are reached
* ``Updater``          self-update

Every base class below has a safe default: a method that an implementation does not override raises
:class:`PlatformUnsupported` with a clear message (fail closed, L1). ``linux.py`` wraps the behaviour the agent had
before the PAL existed; ``windows.py`` fills in what is pure Python and leaves named stubs for later work packages
(W2..W7); ``fake.py`` is the test double.

Use ``get_platform()`` to obtain the active :class:`Platform`. Tests swap it with ``use_platform(...)``.

Names in this module are STABLE: other packages code against them.
"""

from __future__ import annotations

import contextlib
import os
import platform as _stdlib_platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

__all__ = [
    "PLATFORM_NAME",
    "PLATFORM_LINUX",
    "PLATFORM_WINDOWS",
    "AMD_KFD_DEVICE",
    "PlatformUnsupported",
    "Paths",
    "Secrets",
    "ServiceControl",
    "SignalsShutdown",
    "FifoOrPipe",
    "OwnerIdentity",
    "RamWorkdir",
    "HardwareProbe",
    "GpuControl",
    "PowerState",
    "PowerSnapshot",
    "ActivityProbe",
    "ActivitySnapshot",
    "RuntimeBackend",
    "Updater",
    "Platform",
    "get_platform",
    "set_platform",
    "reset_platform",
    "use_platform",
    "detect_platform_name",
]

PLATFORM_LINUX = "linux"
PLATFORM_WINDOWS = "windows"

# The AMD compute device node. A name the runner hands to ``docker --device``; it is a container-runtime argument,
# not a host path the agent reads.
AMD_KFD_DEVICE = "/dev/kfd"


class PlatformUnsupported(RuntimeError):
    """The active platform does not implement this operation (yet). Callers treat it as "refuse", never as "default"."""

    def __init__(self, what: str, *, platform_name: str = "", hint: str = "") -> None:
        self.what = what
        self.platform_name = platform_name
        self.hint = hint
        text = f"{what} is not supported on {platform_name or 'this platform'}"
        if hint:
            text += f": {hint}"
        super().__init__(text)


def detect_platform_name() -> str:
    """'windows' on Windows, 'linux' for everything else this agent supports (the agent never claims another OS)."""
    return PLATFORM_WINDOWS if sys.platform.startswith("win") else PLATFORM_LINUX


# Resolved once from the host OS. ``Platform.name`` is authoritative per instance (the fake can differ).
PLATFORM_NAME: str = detect_platform_name()


class _Unsupported:
    """Mixin: ``self._no(what)`` raises a uniform :class:`PlatformUnsupported`."""

    platform_name: str = ""

    def _no(self, what: str, hint: str = "") -> "PlatformUnsupported":
        return PlatformUnsupported(what, platform_name=self.platform_name, hint=hint)


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #
class Paths(_Unsupported):
    """Where host files live, and the permission primitives that go with them."""

    def config_dir(self) -> Path:
        raise self._no("config_dir")

    def cache_dir(self) -> Path:
        """Default Kit / artifact cache directory."""
        raise self._no("cache_dir")

    def data_dir(self) -> Path:
        """Machine-wide data directory (supervisor logs, state)."""
        raise self._no("data_dir")

    def restrict_dir(self, path: "str | os.PathLike[str]") -> None:
        """Make a directory owner-only (0700 on Linux). Best effort: never raises OSError."""
        raise self._no("restrict_dir")

    def restrict_file(self, path: "str | os.PathLike[str]") -> None:
        """Make a file owner-only read/write (0600 on Linux). Best effort: never raises OSError."""
        raise self._no("restrict_file")

    def set_mode(self, path: "str | os.PathLike[str]", mode: int) -> None:
        """Set POSIX mode bits. Used by the Linux runner for container-visible bits (job dirs, outputs, inputs).
        May raise OSError exactly as ``os.chmod`` does. Windows: PlatformUnsupported (the runner lives in Linux)."""
        raise self._no("set_mode", "the runner executes inside the Linux runtime")


class Secrets(_Unsupported):
    """Credential storage."""

    backend: str = "unsupported"

    def write_file(self, path: "str | os.PathLike[str]", data: bytes) -> None:
        """Store ``data`` at ``path`` readable only by its owner / by the OS secret store."""
        raise self._no("Secrets.write_file")

    def read_file(self, path: "str | os.PathLike[str]") -> bytes:
        raise self._no("Secrets.read_file")


class ServiceControl(_Unsupported):
    """systemd on Linux, the Windows service control manager on Windows."""

    manager: str = "unsupported"

    def is_running_as_service(self) -> bool:
        return False

    def install(self, name: str, argv: list[str], *, description: str = "") -> None:
        raise self._no("ServiceControl.install")

    def uninstall(self, name: str) -> None:
        raise self._no("ServiceControl.uninstall")

    def start(self, name: str) -> None:
        raise self._no("ServiceControl.start")

    def stop(self, name: str) -> None:
        raise self._no("ServiceControl.stop")

    def status(self, name: str) -> str:
        """One of 'running', 'stopped', 'not_installed', 'unknown'."""
        raise self._no("ServiceControl.status")


class SignalsShutdown(_Unsupported):
    """Turn OS stop requests into a callback. The callback receives a reason string such as 'SIGTERM'."""

    def install(self, handler: Callable[[str], None]) -> None:
        raise self._no("SignalsShutdown.install")

    def restore(self) -> None:
        """Put back whatever handlers were in place before ``install``."""
        raise self._no("SignalsShutdown.restore")


class FifoOrPipe(_Unsupported):
    """The private job stream: a FIFO the runner reads without blocking."""

    supported: bool = False

    def create(self, path: "str | os.PathLike[str]", mode: int = 0o622) -> None:
        """Create the pipe at ``path`` (the caller removed any previous file). Raises OSError on failure."""
        raise self._no("FifoOrPipe.create")

    def open_reader(self, path: "str | os.PathLike[str]") -> int:
        """Open the read end non-blocking and return its descriptor."""
        raise self._no("FifoOrPipe.open_reader")


class OwnerIdentity(_Unsupported):
    """Who owns this process."""

    def uid(self) -> Optional[int]:
        """POSIX uid, or None where the concept does not exist."""
        return None

    def user_runtime_dir(self) -> Optional[str]:
        """Per-user runtime directory (``/run/user/<uid>``), or None."""
        return None

    def is_admin(self) -> bool:
        return False


class RamWorkdir(_Unsupported):
    """RAM-only storage for private-tier work (tmpfs). Fails closed: unknown means not RAM."""

    supported: bool = False

    def candidates(self, base: Optional[str] = None) -> list[str]:
        """Directories to try, in order. ``base`` (if given) is the only candidate."""
        raise self._no("RamWorkdir.candidates", "no RAM-only work directory on this platform")

    def mount_type(self, path: str, mounts_file: Optional[str] = None) -> Optional[str]:
        """Filesystem type holding ``path`` (e.g. 'tmpfs'), or None when unknown. ``mounts_file`` overrides the
        mount table location (tests)."""
        raise self._no("RamWorkdir.mount_type")


class HardwareProbe(_Unsupported):
    """Memory, CPU and device probes. Every probe answers False / None / '' when it cannot tell (never guess)."""

    def memory_total_mb(self) -> Optional[int]:
        raise self._no("HardwareProbe.memory_total_mb")

    def cpu_model(self) -> Optional[str]:
        raise self._no("HardwareProbe.cpu_model")

    def cpu_counts(self) -> tuple[int, Optional[int]]:
        """(logical, physical). Physical is None when unknown."""
        raise self._no("HardwareProbe.cpu_counts")

    def cpu_flags_text(self) -> str:
        """Lower-casing is the caller's job. '' when unreadable."""
        return ""

    def dmi_values(self) -> list[str]:
        return []

    def kernel_log(self) -> str:
        return ""

    def has_confidential_device(self) -> bool:
        """AMD SEV-SNP guest/host or Intel TDX guest device present."""
        return False

    def has_tpm_device(self) -> bool:
        return False

    def iommu_active(self, iommu_dir: Optional[str] = None) -> bool:
        return False

    def kernel_lockdown_active(self, lockdown_file: Optional[str] = None) -> bool:
        return False

    def amd_kfd_present(self) -> bool:
        """The AMD ROCm compute device node exists."""
        return False

    def amd_render_node(
        self,
        drm_render_minor: Optional[int],
        bdf: Optional[str],
        *,
        sys_root: Optional[str] = None,
        dev_root: Optional[str] = None,
    ) -> Optional[str]:
        """The render node of one AMD GPU, or None. Never guessed."""
        return None

    def tsm_report_root(self) -> str:
        """Linux configfs TSM report directory (confidential-VM attestation); '' where none exists."""
        return ""

    def kvm_present(self) -> bool:
        return False


class GpuControl(_Unsupported):
    """Thermal and power control surface. Telemetry itself stays in ``telemetry.py`` (NVML / amdsmi)."""

    def power_limit_supported(self) -> bool:
        """True only when this process may change the board power limit (privileges AND a vendor library)."""
        return False

    def set_power_limit(self, watts: float) -> bool:
        """Apply a board power limit to every GPU, best effort. True when at least one GPU was set; never raises."""
        raise self._no("GpuControl.set_power_limit")


@dataclass(frozen=True)
class PowerSnapshot:
    """None means "not known / not applicable"; a caller that needs a known value fails closed."""

    on_battery: Optional[bool] = None
    sleep_pending: Optional[bool] = None
    reboot_pending: Optional[bool] = None


class PowerState(_Unsupported):
    def snapshot(self) -> PowerSnapshot:
        return PowerSnapshot()


@dataclass(frozen=True)
class ActivitySnapshot:
    owner_active: Optional[bool] = None  # recent input or a foreground app using the GPU
    fullscreen_app: Optional[bool] = None
    idle_seconds: Optional[float] = None


class ActivityProbe(_Unsupported):
    def snapshot(self) -> ActivitySnapshot:
        return ActivitySnapshot()


class RuntimeBackend(_Unsupported):
    """How container jobs are reached. ``name`` is what the heartbeat reports as ``platform.runtime_backend``."""

    name: str = "unsupported"

    def docker_command(self) -> list[str]:
        """argv prefix used to invoke the Docker CLI."""
        raise self._no("RuntimeBackend.docker_command")

    def engine_available(self) -> bool:
        return False


class Updater(_Unsupported):
    can_self_update: bool = False

    def current_version(self) -> str:
        raise self._no("Updater.current_version")

    def check(self) -> Optional[str]:
        """Newer version string, or None."""
        raise self._no("Updater.check")

    def apply(self, version: str) -> None:
        raise self._no("Updater.apply")


# --------------------------------------------------------------------------- #
# Platform bundle
# --------------------------------------------------------------------------- #
@dataclass
class Platform:
    """The bundle ``get_platform()`` returns. Members are never None."""

    name: str
    paths: Paths
    secrets: Secrets
    service: ServiceControl
    signals: SignalsShutdown
    fifo: FifoOrPipe
    owner: OwnerIdentity
    ram: RamWorkdir
    hardware: HardwareProbe
    gpu: GpuControl
    power: PowerState
    activity: ActivityProbe
    runtime: RuntimeBackend
    updater: Updater
    # Set by the supervisor (W2) when one is present; reported in the heartbeat ``platform`` object.
    supervisor_version: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def os_version(self) -> str:
        return _stdlib_platform.release() or ""

    def info(self, *, agent_version: Optional[str] = None) -> dict[str, str]:
        """The heartbeat ``platform`` object (plan section 3.4): short strings only, no host identifiers."""
        out: dict[str, str] = {
            "os": self.name,
            "os_version": self.os_version(),
            "runtime_backend": self.runtime.name,
        }
        if agent_version:
            out["agent_version"] = agent_version
        if self.supervisor_version:
            out["supervisor_version"] = self.supervisor_version
        return out


_ACTIVE: Optional[Platform] = None


def _build_native() -> Platform:
    if detect_platform_name() == PLATFORM_WINDOWS:
        from .windows import build_windows_platform

        return build_windows_platform()
    from .linux import build_linux_platform

    return build_linux_platform()


def get_platform() -> Platform:
    """The active platform: the native one unless a test installed another with ``set_platform``/``use_platform``."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = _build_native()
    return _ACTIVE


def set_platform(platform: Platform) -> None:
    global _ACTIVE
    _ACTIVE = platform


def reset_platform() -> None:
    """Forget any override; the next ``get_platform()`` rebuilds the native platform."""
    global _ACTIVE
    _ACTIVE = None


@contextlib.contextmanager
def use_platform(platform: Platform) -> Iterator[Platform]:
    """Install ``platform`` for the duration of a ``with`` block (tests)."""
    global _ACTIVE
    previous = _ACTIVE
    _ACTIVE = platform
    try:
        yield platform
    finally:
        _ACTIVE = previous
