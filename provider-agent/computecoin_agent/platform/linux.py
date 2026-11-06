"""Linux implementation of the Platform Abstraction Layer.

Every method here wraps the behaviour the agent had BEFORE the PAL existed, byte for byte where it matters (modes,
candidate directories, probe paths). Linux behaviour must not change (W0.4). This is the one place that may use
``os.getuid``, ``os.mkfifo``, ``os.chmod``, ``signal.signal`` and ``/proc``, ``/sys``, ``/dev``.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
from pathlib import Path
from shutil import which
from typing import Any, Callable, Optional

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
    PLATFORM_LINUX,
)

_PROC_MOUNTS = "/proc/mounts"
_SEV_PATHS = ("/dev/sev-guest", "/dev/sev", "/sys/module/kvm_amd/parameters/sev_snp")
_TDX_PATHS = ("/dev/tdx-guest", "/dev/tdx_guest")
_TPM_PATHS = ("/dev/tpmrm0", "/dev/tpm0", "/sys/class/tpm/tpm0")
_DMI_FIELDS = ("product_name", "sys_vendor", "board_vendor", "bios_vendor")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


class LinuxPaths(Paths):
    platform_name = PLATFORM_LINUX

    def config_dir(self) -> Path:
        return Path(os.environ.get("COMPUTECOIN_CONFIG_DIR", str(Path.home() / ".computecoin")))

    def cache_dir(self) -> Path:
        return Path.home() / ".computecoin" / "cache"

    def data_dir(self) -> Path:
        return Path.home() / ".computecoin"

    def restrict_dir(self, path: "str | os.PathLike[str]") -> None:
        try:
            os.chmod(path, stat.S_IRWXU)  # 0700
        except OSError:
            pass

    def restrict_file(self, path: "str | os.PathLike[str]") -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def set_mode(self, path: "str | os.PathLike[str]", mode: int) -> None:
        os.chmod(path, mode)


class LinuxSecrets(Secrets):
    platform_name = PLATFORM_LINUX
    backend = "file_0600"

    def write_file(self, path: "str | os.PathLike[str]", data: bytes) -> None:
        # Create with restrictive perms, then write; re-assert the mode in case a umask interfered.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        finally:
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:
                pass

    def read_file(self, path: "str | os.PathLike[str]") -> bytes:
        with open(path, "rb") as fh:
            return fh.read()


class LinuxServiceControl(ServiceControl):
    platform_name = PLATFORM_LINUX
    manager = "systemd"

    def is_running_as_service(self) -> bool:
        # systemd sets INVOCATION_ID for every unit it starts.
        return bool(os.environ.get("INVOCATION_ID"))

    def _unsupported(self, op: str) -> PlatformUnsupported:
        return self._no(f"ServiceControl.{op}", "on Linux the agent runs from a systemd unit installed by the operator script")

    def install(self, name: str, argv: list[str], *, description: str = "") -> None:
        raise self._unsupported("install")

    def uninstall(self, name: str) -> None:
        raise self._unsupported("uninstall")

    def start(self, name: str) -> None:
        raise self._unsupported("start")

    def stop(self, name: str) -> None:
        raise self._unsupported("stop")

    def status(self, name: str) -> str:
        if which("systemctl") is None:
            return "unknown"
        try:
            out = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        text = (out.stdout or "").strip()
        if text == "active":
            return "running"
        if text in ("inactive", "failed", "deactivating"):
            return "stopped"
        return "not_installed" if "could not be found" in (out.stderr or "") else "unknown"


class LinuxSignals(SignalsShutdown):
    platform_name = PLATFORM_LINUX

    def __init__(self) -> None:
        self._previous: dict[int, Any] = {}

    def install(self, handler: Callable[[str], None]) -> None:
        def _on(signum: int, _frame: Any) -> None:
            handler(signal.Signals(signum).name)

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._previous[sig] = signal.signal(sig, _on)

    def restore(self) -> None:
        for sig, prev in list(self._previous.items()):
            signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
        self._previous.clear()


class LinuxFifo(FifoOrPipe):
    platform_name = PLATFORM_LINUX
    supported = True

    def create(self, path: "str | os.PathLike[str]", mode: int = 0o622) -> None:
        os.mkfifo(path, mode)
        os.chmod(path, mode)

    def open_reader(self, path: "str | os.PathLike[str]") -> int:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)


class LinuxOwner(OwnerIdentity):
    platform_name = PLATFORM_LINUX

    def uid(self) -> Optional[int]:
        return os.getuid()

    def user_runtime_dir(self) -> Optional[str]:
        return f"/run/user/{os.getuid()}"

    def is_admin(self) -> bool:
        return os.geteuid() == 0


class LinuxRamWorkdir(RamWorkdir):
    platform_name = PLATFORM_LINUX
    supported = True

    def candidates(self, base: Optional[str] = None) -> list[str]:
        return [base] if base else ["/dev/shm", f"/run/user/{os.getuid()}"]

    def mount_type(self, path: str, mounts_file: Optional[str] = None) -> Optional[str]:
        """Filesystem type of the mount that holds ``path`` (longest matching mount point), or None if unknown."""
        real = os.path.realpath(path)
        best: tuple[int, str] | None = None
        try:
            with open(mounts_file or _PROC_MOUNTS, "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mp = parts[1].replace("\\040", " ")
                    if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
                        if best is None or len(mp) > best[0]:
                            best = (len(mp), parts[2])
        except OSError:
            return None
        return best[1] if best else None


class LinuxHardware(HardwareProbe):
    platform_name = PLATFORM_LINUX

    def memory_total_mb(self) -> Optional[int]:
        """Parse MemTotal from /proc/meminfo. MB or None."""
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // 1024  # "MemTotal:       16308472 kB"
        except (OSError, ValueError, IndexError):
            return None
        return None

    def cpu_model(self) -> Optional[str]:
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            return None
        return None

    def cpu_counts(self) -> tuple[int, Optional[int]]:
        logical = os.cpu_count() or 1
        physical: Optional[int] = None
        try:
            import psutil  # type: ignore

            physical = psutil.cpu_count(logical=False)
        except Exception:
            physical = None
        return logical, physical

    def cpu_flags_text(self) -> str:
        return _read_text("/proc/cpuinfo")

    def dmi_values(self) -> list[str]:
        return [_read_text(f"/sys/class/dmi/id/{f}") for f in _DMI_FIELDS]

    def kernel_log(self) -> str:
        """Boot messages, if this process may read them (needs root or kernel.dmesg_restrict=0)."""
        try:
            out = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5)
            return out.stdout if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def has_confidential_device(self) -> bool:
        return any(os.path.exists(p) for p in _SEV_PATHS + _TDX_PATHS)

    def has_tpm_device(self) -> bool:
        return any(os.path.exists(p) for p in _TPM_PATHS)

    def iommu_active(self, iommu_dir: Optional[str] = None) -> bool:
        try:
            return len(os.listdir(iommu_dir or "/sys/class/iommu")) > 0
        except OSError:
            return False

    def kernel_lockdown_active(self, lockdown_file: Optional[str] = None) -> bool:
        content = _read_text(lockdown_file or "/sys/kernel/security/lockdown")
        return "[integrity]" in content or "[confidentiality]" in content

    def amd_kfd_present(self) -> bool:
        return os.path.exists("/dev/kfd")

    def amd_render_node(
        self,
        drm_render_minor: Optional[int],
        bdf: Optional[str],
        *,
        sys_root: Optional[str] = None,
        dev_root: Optional[str] = None,
    ) -> Optional[str]:
        sys_root = sys_root or "/sys/bus/pci/devices"
        dev_root = dev_root or "/dev/dri"
        if isinstance(drm_render_minor, int) and drm_render_minor >= 128:
            node = os.path.join(dev_root, f"renderD{drm_render_minor}")
            if os.path.exists(node):
                return node
        if bdf:
            try:
                drm_dir = os.path.join(sys_root, str(bdf).strip().lower(), "drm")
                nodes = sorted(d for d in os.listdir(drm_dir) if d.startswith("renderD"))
                if len(nodes) == 1:
                    node = os.path.join(dev_root, nodes[0])
                    if os.path.exists(node):
                        return node
            except Exception:
                pass
        return None

    def tsm_report_root(self) -> str:
        return "/sys/kernel/config/tsm/report"

    def kvm_present(self) -> bool:
        return os.path.exists("/dev/kvm")


class LinuxGpuControl(GpuControl):
    platform_name = PLATFORM_LINUX

    def power_limit_supported(self) -> bool:
        if os.geteuid() != 0:
            return False
        for mod in ("pynvml", "amdsmi"):
            try:
                __import__(mod)
                return True
            except Exception:
                continue
        return False

    def set_power_limit(self, watts: float) -> bool:
        from ..thermal import apply_power_limit

        return apply_power_limit(watts)


class LinuxPower(PowerState):
    platform_name = PLATFORM_LINUX  # no sleep / battery / reboot handling on Linux hosts today: every field stays unknown


class LinuxActivity(ActivityProbe):
    platform_name = PLATFORM_LINUX  # X13 activity probing is optional on Linux: unknown


class LinuxDocker(RuntimeBackend):
    platform_name = PLATFORM_LINUX
    name = "linux_docker"

    def docker_command(self) -> list[str]:
        return ["docker"]

    def engine_available(self) -> bool:
        return which("docker") is not None


class LinuxUpdater(Updater):
    platform_name = PLATFORM_LINUX
    can_self_update = False  # updates are script-managed on Linux


def build_linux_platform() -> Platform:
    return Platform(
        name=PLATFORM_LINUX,
        paths=LinuxPaths(),
        secrets=LinuxSecrets(),
        service=LinuxServiceControl(),
        signals=LinuxSignals(),
        fifo=LinuxFifo(),
        owner=LinuxOwner(),
        ram=LinuxRamWorkdir(),
        hardware=LinuxHardware(),
        gpu=LinuxGpuControl(),
        power=LinuxPower(),
        activity=LinuxActivity(),
        runtime=LinuxDocker(),
        updater=LinuxUpdater(),
    )
