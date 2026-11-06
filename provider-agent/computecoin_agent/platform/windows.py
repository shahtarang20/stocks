"""Windows implementation of the Platform Abstraction Layer (cross-platform plan W0; filled in by W2..W7).

Status: PARTIAL AND UNTESTED ON WINDOWS. Nothing here has been run on a Windows machine.

* Implemented (pure Python): ``Paths`` (``%ProgramData%`` / ``%LOCALAPPDATA%``), ``HardwareProbe`` memory and CPU via
  ``psutil``, ``OwnerIdentity`` (admin check), ``SignalsShutdown`` (console control events through ``signal``).
* Stubs that raise :class:`PlatformUnsupported` with the owning work package named, so a caller fails closed:
  ``Secrets`` (W2: DPAPI / Credential Manager, X7), ``ServiceControl`` (W2), ``FifoOrPipe`` (the runner is in Linux),
  ``RamWorkdir`` (no private tier on Windows, X5), ``GpuControl`` (W7), ``PowerState`` / ``ActivityProbe`` (W5),
  ``RuntimeBackend`` (W3/W4: ``wsl_engine``), ``Updater`` (W6).

A Windows host reports only container-tier capability flags (X5); the probes that would advertise a privacy tier
(TPM, confidential devices, gVisor, microVM) deliberately answer False here.

Docs to verify before a package replaces a stub (X11): WSL (https://learn.microsoft.com/windows/wsl/), Windows service
APIs, DPAPI (https://learn.microsoft.com/windows/win32/api/dpapi/), NVML on Windows.
"""

from __future__ import annotations

import os
import platform as _stdlib_platform
import signal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import (
    ActivityProbe,
    FifoOrPipe,
    GpuControl,
    HardwareProbe,
    OwnerIdentity,
    Paths,
    Platform,
    PlatformUnsupported,
    PowerState,
    RamWorkdir,
    RuntimeBackend,
    Secrets,
    ServiceControl,
    SignalsShutdown,
    Updater,
    PLATFORM_WINDOWS,
)

_VENDOR = "ComputeCoin"


class WindowsPaths(Paths):
    """Host files live under ``%ProgramData%\\ComputeCoin`` (machine-wide, service account) and
    ``%LOCALAPPDATA%\\ComputeCoin`` (cache). The runner's own paths stay POSIX inside the distro (X9)."""

    platform_name = PLATFORM_WINDOWS

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self._env = env if env is not None else os.environ

    def _program_data(self) -> Path:
        base = self._env.get("ProgramData") or self._env.get("PROGRAMDATA")
        return Path(base) if base else Path("C:/ProgramData")

    def _local_app_data(self) -> Path:
        base = self._env.get("LOCALAPPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Local"

    def config_dir(self) -> Path:
        override = self._env.get("COMPUTECOIN_CONFIG_DIR")
        return Path(override) if override else self._program_data() / _VENDOR

    def cache_dir(self) -> Path:
        return self._local_app_data() / _VENDOR / "cache"

    def data_dir(self) -> Path:
        return self._program_data() / _VENDOR

    def restrict_dir(self, path: "str | os.PathLike[str]") -> None:
        raise self._no("Paths.restrict_dir", "Windows needs an ACL, not a Unix mode (W2)")

    def restrict_file(self, path: "str | os.PathLike[str]") -> None:
        raise self._no("Paths.restrict_file", "Windows needs an ACL, not a Unix mode (W2)")


class WindowsSecrets(Secrets):
    platform_name = PLATFORM_WINDOWS
    backend = "dpapi"  # target backend; not implemented yet

    def write_file(self, path: "str | os.PathLike[str]", data: bytes) -> None:
        raise self._no("Secrets.write_file", "DPAPI / Credential Manager storage is work package W2 (X7); never a plain file")

    def read_file(self, path: "str | os.PathLike[str]") -> bytes:
        raise self._no("Secrets.read_file", "DPAPI / Credential Manager storage is work package W2 (X7)")


class WindowsServiceControl(ServiceControl):
    platform_name = PLATFORM_WINDOWS
    manager = "windows_scm"

    def _w2(self, op: str) -> PlatformUnsupported:
        return self._no(f"ServiceControl.{op}", "the Windows Service is work package W2")

    def install(self, name: str, argv: list[str], *, description: str = "") -> None:
        raise self._w2("install")

    def uninstall(self, name: str) -> None:
        raise self._w2("uninstall")

    def start(self, name: str) -> None:
        raise self._w2("start")

    def stop(self, name: str) -> None:
        raise self._w2("stop")

    def status(self, name: str) -> str:
        raise self._w2("status")


class WindowsSignals(SignalsShutdown):
    """Console control events via ``signal`` (SIGINT, SIGBREAK, SIGTERM). Service-stop events are W2."""

    platform_name = PLATFORM_WINDOWS

    def __init__(self) -> None:
        self._previous: dict[Any, Any] = {}

    def install(self, handler: Callable[[str], None]) -> None:
        def _on(signum: int, _frame: Any) -> None:
            handler(signal.Signals(signum).name)

        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                self._previous[sig] = signal.signal(sig, _on)

    def restore(self) -> None:
        for sig, prev in list(self._previous.items()):
            signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
        self._previous.clear()


class WindowsFifo(FifoOrPipe):
    platform_name = PLATFORM_WINDOWS
    supported = False

    def create(self, path: "str | os.PathLike[str]", mode: int = 0o622) -> None:
        raise self._no("FifoOrPipe.create", "the runner (and its private stream) lives in the Linux runtime, not on Windows")

    def open_reader(self, path: "str | os.PathLike[str]") -> int:
        raise self._no("FifoOrPipe.open_reader", "the runner lives in the Linux runtime")


class WindowsOwner(OwnerIdentity):
    platform_name = PLATFORM_WINDOWS

    def is_admin(self) -> bool:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False


class WindowsRamWorkdir(RamWorkdir):
    platform_name = PLATFORM_WINDOWS
    supported = False

    def candidates(self, base: Optional[str] = None) -> list[str]:
        raise self._no("RamWorkdir.candidates", "private-tier jobs are not offered on Windows hosts (X5)")

    def mount_type(self, path: str, mounts_file: Optional[str] = None) -> Optional[str]:
        raise self._no("RamWorkdir.mount_type", "private-tier jobs are not offered on Windows hosts (X5)")


class WindowsHardware(HardwareProbe):
    """Memory and CPU through ``psutil``. Every privacy-relevant probe stays False (X5)."""

    platform_name = PLATFORM_WINDOWS

    def memory_total_mb(self) -> Optional[int]:
        try:
            import psutil

            return int(psutil.virtual_memory().total // (1024 * 1024))
        except Exception:
            return None

    def cpu_model(self) -> Optional[str]:
        return _stdlib_platform.processor() or None

    def cpu_counts(self) -> tuple[int, Optional[int]]:
        logical = os.cpu_count() or 1
        try:
            import psutil

            return logical, psutil.cpu_count(logical=False)
        except Exception:
            return logical, None

    def cpu_flags_text(self) -> str:
        # Fail closed: a Windows host with WSL2 / virtualization-based security runs under a hypervisor, so it is never
        # reported as bare metal (a privacy-tier input that Windows hosts do not qualify for anyway, X5).
        return " hypervisor"


class WindowsGpuControl(GpuControl):
    platform_name = PLATFORM_WINDOWS

    def set_power_limit(self, watts: float) -> bool:
        raise self._no("GpuControl.set_power_limit", "native NVML power limit from the supervisor is work package W7")


class WindowsPower(PowerState):
    platform_name = PLATFORM_WINDOWS
    # TODO(W5): sleep / hibernate / reboot-pending / battery. Until then every field is unknown (None).


class WindowsActivity(ActivityProbe):
    platform_name = PLATFORM_WINDOWS
    # TODO(W5): foreground app, last input, full-screen detection. Until then unknown (None).


class WindowsWslEngine(RuntimeBackend):
    platform_name = PLATFORM_WINDOWS
    name = "wsl_engine"

    def docker_command(self) -> list[str]:
        raise self._no("RuntimeBackend.docker_command", "the managed WSL2 distro is work package W3 (X2)")


class WindowsUpdater(Updater):
    platform_name = PLATFORM_WINDOWS  # TODO(W6): signed updater with rollback.


def build_windows_platform(env: Optional[Mapping[str, str]] = None) -> Platform:
    return Platform(
        name=PLATFORM_WINDOWS,
        paths=WindowsPaths(env),
        secrets=WindowsSecrets(),
        service=WindowsServiceControl(),
        signals=WindowsSignals(),
        fifo=WindowsFifo(),
        owner=WindowsOwner(),
        ram=WindowsRamWorkdir(),
        hardware=WindowsHardware(),
        gpu=WindowsGpuControl(),
        power=WindowsPower(),
        activity=WindowsActivity(),
        runtime=WindowsWslEngine(),
        updater=WindowsUpdater(),
    )
