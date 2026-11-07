"""Windows implementation of the Platform Abstraction Layer (cross-platform plan W0; filled in by W2..W7).

Status: PARTIAL AND UNTESTED ON WINDOWS. Nothing here has been run on a Windows machine.

* Implemented (pure Python): ``Paths`` (``%ProgramData%`` / ``%LOCALAPPDATA%``), ``HardwareProbe`` memory and CPU via
  ``psutil``, ``OwnerIdentity`` (admin check), ``SignalsShutdown`` (console control events through ``signal``).
* DELEGATING to the packages that implement them, through lazy imports (so this module still imports on Linux, and a
  missing pywin32 or a non-Windows host surfaces as :class:`PlatformUnsupported`, never as an ImportError at import):
  ``Paths.restrict_*`` -> ``windows/paths.py`` (icacls ACLs), ``Secrets`` -> ``windows/secrets_dpapi.py`` (DPAPI),
  ``ServiceControl`` -> ``windows/service.py``, ``PowerState`` / ``ActivityProbe`` -> ``windows/governance/probes.py``,
  ``RuntimeBackend`` -> ``windows/runtime/*`` (``wsl.exe`` argv builders, the managed distro), ``Updater`` ->
  ``updater/pal.py`` (attached by the supervisor).
* Stubs that raise :class:`PlatformUnsupported` with the owning work package named, so a caller fails closed:
  ``FifoOrPipe`` (the runner is in Linux), ``RamWorkdir`` (no private tier on Windows, X5), ``GpuControl`` (W7).

A Windows host reports only container-tier capability flags (X5); the probes that would advertise a privacy tier
(TPM, confidential devices, gVisor, microVM) deliberately answer False here.

Docs to verify before a package replaces a stub (X11): WSL (https://learn.microsoft.com/windows/wsl/), Windows service
APIs, DPAPI (https://learn.microsoft.com/windows/win32/api/dpapi/), NVML on Windows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import platform as _stdlib_platform
import re
import secrets as _secrets
import signal
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

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
    PLATFORM_WINDOWS,
)

_VENDOR = "ComputeCoin"


def _on_windows() -> bool:
    """True when the Win32-backed delegates can work. Everywhere else they fail closed with PlatformUnsupported (an icacls
    call or a DPAPI blob made on a non-Windows machine would be meaningless). A function, so tests can patch it."""
    return sys.platform == "win32"


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
        """ACL the directory to SYSTEM and Administrators (``windows/paths.py: restrict_path``, icacls). Unlike the Linux
        chmod this is NOT best effort: a directory that cannot be locked down raises ``PathSecurityError`` so nothing
        sensitive is written into it."""
        if not _on_windows():
            raise self._no("Paths.restrict_dir", "icacls is only available on Windows")
        from ..windows.paths import restrict_path

        restrict_path(path)

    def restrict_file(self, path: "str | os.PathLike[str]") -> None:
        """Same ACL, applied to the file itself (it does not rely on the parent's inheritance)."""
        if not _on_windows():
            raise self._no("Paths.restrict_file", "icacls is only available on Windows")
        from ..windows.paths import restrict_path

        restrict_path(path)


class WindowsSecrets(Secrets):
    """DPAPI (with a Credential Manager fallback) through ``windows/secrets_dpapi.py``.

    * An agent config (``*.toml``, what ``AgentConfig.save`` writes): the secret keys (``machine_secret``,
      ``signing_secret``, ``enrollment_token``) go to the DPAPI store in ``<dir>\\secrets`` and the file at ``path`` is
      written WITHOUT them (atomically, inside the ACL-restricted directory), which closes the window in which the
      credential would sit in a plain file. If the store fails the full file is kept (the only copy of a single-use
      credential must not be destroyed): ``windows/enrol.py`` then reports it and ``service migrate`` finishes the job.
    * Any other file: a base64-wrapped DPAPI blob ``<name>.dpapi`` next to ``path``; never a plain file.
    """

    platform_name = PLATFORM_WINDOWS
    backend = "dpapi"

    @staticmethod
    def _secret_name(path: "str | os.PathLike[str]") -> str:
        """A store-legal name (``[a-z][a-z0-9_]*``) that is stable for one file name and distinct across file names."""
        filename = Path(path).name
        slug = re.sub(r"[^a-z0-9_]", "_", filename.lower())[:40]
        return "f_" + slug + "_" + hashlib.sha256(filename.encode("utf-8")).hexdigest()[:12]

    def _store(self, directory: "str | os.PathLike[str]") -> Any:
        if not _on_windows():
            raise self._no("Secrets", "DPAPI is only available on Windows")
        try:
            from ..windows.paths import restrict_path
            from ..windows.secrets_dpapi import DpapiSecretStore
        except ImportError as exc:  # tomli_w missing, etc.
            raise self._no("Secrets", f"the DPAPI store could not be loaded ({type(exc).__name__})") from None
        return DpapiSecretStore(directory, prepare_directory=restrict_path)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        tmp = path.with_name(path.name + "." + _secrets.token_hex(4) + ".tmp")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    @staticmethod
    def _is_config(path: Path) -> bool:
        return path.suffix.lower() == ".toml"

    def write_file(self, path: "str | os.PathLike[str]", data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("secret data must be non-empty bytes")
        target = Path(path)
        if not self._is_config(target):
            self._store(target.parent).store(self._secret_name(target), base64.b64encode(bytes(data)).decode("ascii"))
            return
        import tomli_w

        from ..windows.secrets_dpapi import SECRET_CONFIG_KEYS, SecretStoreError

        store = self._store(target.parent / "secrets")  # fails closed (PlatformUnsupported) before anything is written
        try:
            values = tomllib.loads(bytes(data).decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise ValueError("the config is not valid TOML") from None
        secret_values = {k: values[k] for k in SECRET_CONFIG_KEYS if isinstance(values.get(k), str) and values[k]}
        payload = bytes(data)
        if secret_values:
            try:
                for key, value in secret_values.items():
                    store.store(key, value)
                    back = store.load(key)
                    if back is None or not hmac.compare_digest(back.encode("utf-8"), value.encode("utf-8")):
                        raise SecretStoreError(f"{key} did not round-trip through the secret store")
                payload = tomli_w.dumps({k: v for k, v in values.items() if k not in SECRET_CONFIG_KEYS}).encode("utf-8")
            except SecretStoreError:
                payload = bytes(data)  # keep the only copy inside the restricted directory (see the class docstring)
        self._atomic_write(target, payload)

    def read_file(self, path: "str | os.PathLike[str]") -> bytes:
        target = Path(path)
        if not self._is_config(target):
            value = self._store(target.parent).load(self._secret_name(target))
            if value is None:
                raise FileNotFoundError(str(target))
            return base64.b64decode(value.encode("ascii"), validate=True)
        import tomli_w

        from ..windows.secrets_dpapi import SECRET_CONFIG_KEYS

        store = self._store(target.parent / "secrets")
        values = tomllib.loads(target.read_bytes().decode("utf-8"))  # FileNotFoundError when there is no config
        for key in SECRET_CONFIG_KEYS:
            if key not in values:
                found = store.load(key)
                if found:
                    values[key] = found
        return tomli_w.dumps(values).encode("utf-8")


class WindowsServiceControl(ServiceControl):
    """The Windows service control manager, through ``windows/service.py`` (pywin32). It manages ONE service, the
    ComputeCoin supervisor: ``name`` must be that service's name, and ``argv`` is ignored because the service class
    (``computecoin_agent.windows.service``) defines its own entry point."""

    platform_name = PLATFORM_WINDOWS
    manager = "windows_scm"

    def _manager(self, name: str) -> Any:
        from ..windows.service import SERVICE_NAME, ServiceManagerError, load_service_manager

        if name != SERVICE_NAME:
            raise self._no("ServiceControl", f"only the {SERVICE_NAME!r} service is managed here")
        try:
            return load_service_manager()
        except ServiceManagerError as exc:  # pywin32 is not installed
            raise self._no("ServiceControl", str(exc)) from None

    def install(self, name: str, argv: list[str], *, description: str = "") -> None:
        self._manager(name).install()

    def uninstall(self, name: str) -> None:
        self._manager(name).remove()

    def start(self, name: str) -> None:
        self._manager(name).start()

    def stop(self, name: str) -> None:
        self._manager(name).stop()

    def status(self, name: str) -> str:
        """``running`` | ``stopped`` | ``not_installed`` | ``unknown`` (plus the transitional states the manager reports)."""
        return self._manager(name).status()


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
    """Battery, reboot-pending and the suspend latch from ``windows/governance/probes.py: WindowsPowerState`` (works from
    a service). Where the Win32 API is unavailable (not Windows) every field is unknown (None), which the governance
    policy treats as "block"."""

    platform_name = PLATFORM_WINDOWS

    def __init__(self, delegate: Any = None) -> None:
        self._delegate = delegate

    def snapshot(self) -> PowerSnapshot:
        if self._delegate is None:
            from ..windows.governance import probes
            from ..windows.governance.policy import ProbeUnsupported

            try:
                self._delegate = probes.WindowsPowerState()
            except ProbeUnsupported:
                return PowerSnapshot()
        return self._delegate.snapshot()


class WindowsActivity(ActivityProbe):
    """Idle time and full-screen state from ``windows/governance/probes.py: WindowsActivityProbe``. These calls describe the
    INTERACTIVE session: from the service (session 0) use ``governance/snapshot.py: SnapshotProbe`` fed by the session
    helper instead (the supervisor does). TODO(verify-docs): behaviour of each call when run in session 0."""

    platform_name = PLATFORM_WINDOWS

    def __init__(self, delegate: Any = None) -> None:
        self._delegate = delegate

    def snapshot(self) -> ActivitySnapshot:
        if self._delegate is None:
            from ..windows.governance import probes
            from ..windows.governance.policy import ProbeUnsupported

            try:
                self._delegate = probes.WindowsActivityProbe()
            except ProbeUnsupported:
                return ActivitySnapshot()
        return self._delegate.snapshot()


class WindowsWslEngine(RuntimeBackend):
    """The managed WSL2 distro (``windows/runtime/*``): Docker is reached with ``wsl.exe --distribution computecoin
    --user ccrunner --exec docker`` (argv list, no shell)."""

    platform_name = PLATFORM_WINDOWS
    name = "wsl_engine"

    def docker_command(self) -> list[str]:
        from ..windows.runtime import distro_paths as P
        from ..windows.runtime.wsl import argv_exec

        return argv_exec(P.DISTRO_NAME, ["docker"], user=P.RUNNER_USER)

    def engine_available(self) -> bool:
        """True only when the managed distro is registered as WSL2 AND its Docker engine answers. Never raises."""
        try:
            from ..windows.runtime import distro_paths as P
            from ..windows.runtime.wsl import WslCli, subprocess_runner

            cli = WslCli(subprocess_runner, P.DISTRO_NAME)
            info = cli.distro_info()
            if info is None or info.version != 2:
                return False
            return cli.exec(["docker", "version", "--format", "{{.Server.Version}}"], user=P.RUNNER_USER, timeout=30).ok
        except Exception:
            return False


class WindowsUpdater(Updater):
    """Delegates to ``updater/pal.py: AgentPalUpdater`` once the supervisor has attached its ``AgentUpdater``
    (``attach``); before that ``check`` / ``apply`` raise PlatformUnsupported (no updater is running in this process)."""

    platform_name = PLATFORM_WINDOWS

    def __init__(self) -> None:
        self._delegate: Any = None

    @property  # type: ignore[override]
    def can_self_update(self) -> bool:
        return self._delegate is not None

    def attach(self, agent_updater: Any) -> None:
        from ..updater.pal import AgentPalUpdater

        self._delegate = AgentPalUpdater(agent_updater)

    def current_version(self) -> str:
        if self._delegate is not None:
            return self._delegate.current_version()
        from .. import __version__

        return __version__

    def check(self) -> Optional[str]:
        if self._delegate is None:
            raise self._no("Updater.check", "the supervisor's updater is not attached to this process")
        return self._delegate.check()

    def apply(self, version: str) -> None:
        if self._delegate is None:
            raise self._no("Updater.apply", "the supervisor's updater is not attached to this process")
        self._delegate.apply(version)


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
