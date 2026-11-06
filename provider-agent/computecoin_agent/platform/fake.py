"""Test double for the Platform Abstraction Layer.

``make_fake_platform()`` returns a :class:`Platform` whose every member is controllable and records what was asked, so a
test can run any agent code path as if on Linux or Windows without touching the real OS. Nothing here reads ``/proc``,
``/sys`` or ``/dev`` and nothing calls ``os.chmod``.

    from computecoin_agent.platform import use_platform
    from computecoin_agent.platform.fake import make_fake_platform

    with use_platform(make_fake_platform(name="windows", memory_mb=16384)):
        ...
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from . import (
    ActivityProbe,
    ActivitySnapshot,
    FifoOrPipe,
    GpuControl,
    HardwareProbe,
    OwnerIdentity,
    Paths,
    Platform,
    PlatformUnsupported,
    PowerSnapshot,
    PowerState,
    RamWorkdir,
    RuntimeBackend,
    Secrets,
    ServiceControl,
    SignalsShutdown,
    Updater,
    PLATFORM_LINUX,
)


class FakePaths(Paths):
    def __init__(self, root: "Path | str" = "/fake-root") -> None:
        self.root = Path(root)
        self.restricted_dirs: list[str] = []
        self.restricted_files: list[str] = []
        self.modes: list[tuple[str, int]] = []

    def config_dir(self) -> Path:
        return self.root / "config"

    def cache_dir(self) -> Path:
        return self.root / "cache"

    def data_dir(self) -> Path:
        return self.root / "data"

    def restrict_dir(self, path: "str | os.PathLike[str]") -> None:
        self.restricted_dirs.append(str(path))

    def restrict_file(self, path: "str | os.PathLike[str]") -> None:
        self.restricted_files.append(str(path))

    def set_mode(self, path: "str | os.PathLike[str]", mode: int) -> None:
        self.modes.append((str(path), mode))


class FakeSecrets(Secrets):
    """In-memory store: nothing is written to disk."""

    backend = "memory"

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def write_file(self, path: "str | os.PathLike[str]", data: bytes) -> None:
        self.store[str(path)] = bytes(data)

    def read_file(self, path: "str | os.PathLike[str]") -> bytes:
        try:
            return self.store[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None


class FakeService(ServiceControl):
    manager = "fake"

    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.as_service = False

    def is_running_as_service(self) -> bool:
        return self.as_service

    def install(self, name: str, argv: list[str], *, description: str = "") -> None:
        self.calls.append(("install", name))
        self.state[name] = "stopped"

    def uninstall(self, name: str) -> None:
        self.calls.append(("uninstall", name))
        self.state.pop(name, None)

    def start(self, name: str) -> None:
        self.calls.append(("start", name))
        self.state[name] = "running"

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))
        self.state[name] = "stopped"

    def status(self, name: str) -> str:
        return self.state.get(name, "not_installed")


class FakeSignals(SignalsShutdown):
    """No real signal is registered; ``fire`` delivers one to the installed handler."""

    def __init__(self) -> None:
        self.handler: Optional[Callable[[str], None]] = None
        self.restored = False

    def install(self, handler: Callable[[str], None]) -> None:
        self.handler = handler
        self.restored = False

    def restore(self) -> None:
        self.handler = None
        self.restored = True

    def fire(self, reason: str = "SIGTERM") -> None:
        if self.handler is None:
            raise AssertionError("no shutdown handler installed")
        self.handler(reason)


class FakeFifo(FifoOrPipe):
    """An in-memory 'pipe': ``create`` records the path; ``fail`` makes it raise OSError like a real failure."""

    supported = True

    def __init__(self) -> None:
        self.created: list[tuple[str, int]] = []
        self.fail = False
        self._next_fd = 1000

    def create(self, path: "str | os.PathLike[str]", mode: int = 0o622) -> None:
        if self.fail:
            raise OSError("fake fifo failure")
        self.created.append((str(path), mode))

    def open_reader(self, path: "str | os.PathLike[str]") -> int:
        if self.fail:
            raise OSError("fake fifo failure")
        self._next_fd += 1
        return self._next_fd


class FakeOwner(OwnerIdentity):
    def __init__(self, uid: Optional[int] = 1000, runtime_dir: Optional[str] = None, admin: bool = False) -> None:
        self._uid = uid
        self._runtime_dir = runtime_dir if runtime_dir is not None else (f"/run/user/{uid}" if uid is not None else None)
        self._admin = admin

    def uid(self) -> Optional[int]:
        return self._uid

    def user_runtime_dir(self) -> Optional[str]:
        return self._runtime_dir

    def is_admin(self) -> bool:
        return self._admin


class FakeRam(RamWorkdir):
    """``mounts`` maps a path prefix to a filesystem type; the longest matching prefix wins."""

    def __init__(self, mounts: Optional[dict[str, str]] = None, supported: bool = True, dirs: Optional[list[str]] = None) -> None:
        self.mounts = dict(mounts or {})
        self.supported = supported
        self.dirs = list(dirs or [])

    def candidates(self, base: Optional[str] = None) -> list[str]:
        if not self.supported:
            raise PlatformUnsupported("RamWorkdir.candidates", platform_name="fake")
        return [base] if base else list(self.dirs)

    def mount_type(self, path: str, mounts_file: Optional[str] = None) -> Optional[str]:
        if not self.supported:
            raise PlatformUnsupported("RamWorkdir.mount_type", platform_name="fake")
        best: Optional[tuple[int, str]] = None
        for prefix, fstype in self.mounts.items():
            p = prefix.rstrip("/") or "/"
            if path == p or path.startswith(p.rstrip("/") + "/") or p == "/":
                if best is None or len(p) > best[0]:
                    best = (len(p), fstype)
        return best[1] if best else None


class FakeHardware(HardwareProbe):
    def __init__(
        self,
        *,
        memory_mb: Optional[int] = 8192,
        cpu_model: Optional[str] = "Fake CPU",
        logical: int = 8,
        physical: Optional[int] = 4,
        cpuinfo: str = "",
        dmi: Optional[list[str]] = None,
        kernel_log: str = "",
        confidential_device: bool = False,
        tpm: bool = False,
        iommu: bool = False,
        lockdown: bool = False,
        kfd: bool = False,
        kvm: bool = False,
        tsm_root: str = "",
        render_node: Optional[str] = None,
    ) -> None:
        self._memory_mb = memory_mb
        self._cpu_model = cpu_model
        self._counts = (logical, physical)
        self._cpuinfo = cpuinfo
        self._dmi = list(dmi or [])
        self._kernel_log = kernel_log
        self._cc = confidential_device
        self._tpm = tpm
        self._iommu = iommu
        self._lockdown = lockdown
        self._kfd = kfd
        self._kvm = kvm
        self._tsm_root = tsm_root
        self._render_node = render_node

    def memory_total_mb(self) -> Optional[int]:
        return self._memory_mb

    def cpu_model(self) -> Optional[str]:
        return self._cpu_model

    def cpu_counts(self) -> tuple[int, Optional[int]]:
        return self._counts

    def cpu_flags_text(self) -> str:
        return self._cpuinfo

    def dmi_values(self) -> list[str]:
        return list(self._dmi)

    def kernel_log(self) -> str:
        return self._kernel_log

    def has_confidential_device(self) -> bool:
        return self._cc

    def has_tpm_device(self) -> bool:
        return self._tpm

    def iommu_active(self, iommu_dir: Optional[str] = None) -> bool:
        return self._iommu

    def kernel_lockdown_active(self, lockdown_file: Optional[str] = None) -> bool:
        return self._lockdown

    def amd_kfd_present(self) -> bool:
        return self._kfd

    def amd_render_node(self, drm_render_minor: Optional[int], bdf: Optional[str], *, sys_root: Optional[str] = None, dev_root: Optional[str] = None) -> Optional[str]:
        return self._render_node

    def tsm_report_root(self) -> str:
        return self._tsm_root

    def kvm_present(self) -> bool:
        return self._kvm


class FakeGpu(GpuControl):
    def __init__(self, supported: bool = False) -> None:
        self._supported = supported
        self.limits: list[float] = []

    def power_limit_supported(self) -> bool:
        return self._supported

    def set_power_limit(self, watts: float) -> bool:
        self.limits.append(watts)
        return self._supported


class FakePower(PowerState):
    def __init__(self, snap: Optional[PowerSnapshot] = None) -> None:
        self.snap = snap or PowerSnapshot()

    def snapshot(self) -> PowerSnapshot:
        return self.snap


class FakeActivity(ActivityProbe):
    def __init__(self, snap: Optional[ActivitySnapshot] = None) -> None:
        self.snap = snap or ActivitySnapshot()

    def snapshot(self) -> ActivitySnapshot:
        return self.snap


class FakeRuntime(RuntimeBackend):
    def __init__(self, name: str = "fake_docker", available: bool = True) -> None:
        self.name = name
        self._available = available

    def docker_command(self) -> list[str]:
        return ["docker"]

    def engine_available(self) -> bool:
        return self._available


class FakeUpdater(Updater):
    can_self_update = True

    def __init__(self, version: str = "0.0.0", newer: Optional[str] = None) -> None:
        self.version = version
        self.newer = newer
        self.applied: list[str] = []

    def current_version(self) -> str:
        return self.version

    def check(self) -> Optional[str]:
        return self.newer

    def apply(self, version: str) -> None:
        self.applied.append(version)
        self.version = version


def make_fake_platform(
    name: str = PLATFORM_LINUX,
    *,
    root: "Path | str" = "/fake-root",
    memory_mb: Optional[int] = 8192,
    runtime_name: Optional[str] = None,
    docker_available: bool = True,
    supervisor_version: Optional[str] = None,
    **hardware_kwargs: Any,
) -> Platform:
    """A fully faked platform. ``name`` ('linux' or 'windows') is what ``Platform.name`` reports; extra keyword
    arguments configure :class:`FakeHardware`. Replace any member on the result to customise further."""
    if runtime_name is None:
        runtime_name = "wsl_engine" if name == "windows" else "linux_docker"
    return Platform(
        name=name,
        paths=FakePaths(root),
        secrets=FakeSecrets(),
        service=FakeService(),
        signals=FakeSignals(),
        fifo=FakeFifo(),
        owner=FakeOwner(),
        ram=FakeRam(),
        hardware=FakeHardware(memory_mb=memory_mb, **hardware_kwargs),
        gpu=FakeGpu(),
        power=FakePower(),
        activity=FakeActivity(),
        runtime=FakeRuntime(runtime_name, docker_available),
        updater=FakeUpdater(),
        supervisor_version=supervisor_version,
    )
