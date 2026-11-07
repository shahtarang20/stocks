"""Windows probes for resource governance (cross-platform plan W5, X13).

Implements the PAL ``ActivityProbe`` and ``PowerState`` interfaces and the owner-GPU probe used by
:mod:`computecoin_agent.windows.governance.policy`, with documented Win32 calls through ``ctypes``.

WRITTEN, NEVER RUN ON WINDOWS. Every Win32, NVML and registry detail below is marked ``TODO(verify-docs)`` (X11): it
has to be checked against the current Microsoft and NVIDIA documentation, with URL and date recorded, before use.

Importing this module is safe on any OS: no Win32 call happens at import. ``Win32Api`` is the only class that touches
``ctypes.windll``; everything else takes an injectable ``api`` so the logic is unit-tested on Linux with fakes.

SESSION 0 CAVEAT (important): ``GetLastInputInfo``, ``GetForegroundWindow`` and ``SHQueryUserNotificationState`` describe
the INTERACTIVE desktop session. A Windows Service lives in session 0 and sees none of it. The activity probe therefore
runs in a helper process in the user's session (``python -m computecoin_agent.windows.governance.probes
--snapshot-json``) and the service reads its reports through ``snapshot.SnapshotProbe``. ``SetThreadExecutionState`` and
the power/registry probes work from the service itself. TODO(verify-docs): confirm session 0 behaviour of each call.

Doc pages to check (record URL + date when done):
* GetLastInputInfo / LASTINPUTINFO            learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getlastinputinfo
* GetTickCount                                learn.microsoft.com/windows/win32/api/sysinfoapi/nf-sysinfoapi-gettickcount
* GetForegroundWindow, GetWindowRect          learn.microsoft.com/windows/win32/api/winuser/
* MonitorFromWindow, GetMonitorInfoW          learn.microsoft.com/windows/win32/api/winuser/nf-winuser-monitorfromwindow
* SHQueryUserNotificationState                learn.microsoft.com/windows/win32/api/shellapi/nf-shellapi-shqueryusernotificationstate
* QueryFullProcessImageNameW, OpenProcess     learn.microsoft.com/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew
* SetThreadExecutionState                     learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
* GetSystemPowerStatus / SYSTEM_POWER_STATUS  learn.microsoft.com/windows/win32/api/winbase/nf-winbase-getsystempowerstatus
* WTSGetActiveConsoleSessionId                learn.microsoft.com/windows/win32/api/winbase/nf-winbase-wtsgetactiveconsolesessionid
* Reboot-pending registry keys                (community-documented, NOT a stable Microsoft contract: see REBOOT_KEYS)
* NVML process lists / memory info            docs.nvidia.com/deploy/nvml-api (WDDM may report no per-process memory)
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ...platform import ActivityProbe, ActivitySnapshot, PowerSnapshot, PowerState
from .policy import ProbeUnsupported
from .snapshot import encode_snapshot

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# ---- constants (TODO(verify-docs) each against winuser.h / winbase.h / shellapi.h) ----
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001  # keep the SYSTEM awake; the display may still turn off (we never ask for ES_DISPLAY_REQUIRED)
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
NO_ACTIVE_CONSOLE_SESSION = 0xFFFFFFFF
AC_LINE_OFFLINE, AC_LINE_ONLINE, AC_LINE_UNKNOWN = 0, 1, 255
BATTERY_FLAG_NO_BATTERY = 128
BATTERY_FLAG_UNKNOWN = 255
# SHQUERYUSERNOTIFICATIONSTATE: QUNS_BUSY=2 (a full-screen app), QUNS_RUNNING_D3D_FULL_SCREEN=3, QUNS_PRESENTATION_MODE=4
FULLSCREEN_NOTIFICATION_STATES = frozenset({2, 3, 4})
SHELL_WINDOW_CLASSES = frozenset({"progman", "workerw", "shell_traywnd"})  # the desktop itself is not a full-screen app

# Power broadcast codes the SERVICE handler receives (SERVICE_CONTROL_POWEREVENT, PBT_*). TODO(verify-docs)
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

# Reboot-pending evidence. Existence of the first two KEYS is the widely used signal; PendingFileRenameOperations is
# noisy (many installers set it) so it is opt-in. TODO(verify-docs): none of these is a documented stable interface.
REBOOT_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
)
PENDING_RENAME_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager"
PENDING_RENAME_VALUE = "PendingFileRenameOperations"


@dataclasses.dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    class_name: str
    rect: tuple  # (left, top, right, bottom) in virtual-screen pixels
    monitor_rect: tuple  # the monitor the window is mostly on
    is_shell: bool = False  # the desktop / shell window


# --------------------------------------------------------------------------- #
# The only code that calls Win32
# --------------------------------------------------------------------------- #
class Win32Api:
    """Thin ctypes wrapper. Every method returns plain Python values and raises ``ProbeUnsupported`` off Windows."""

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise ProbeUnsupported("Win32 is only available on Windows")
        import ctypes
        from ctypes import wintypes

        self._ct, self._wt = ctypes, wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

        self._LASTINPUTINFO, self._MONITORINFO, self._SPS = LASTINPUTINFO, MONITORINFO, SYSTEM_POWER_STATUS
        u, k = self._user32, self._kernel32
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HANDLE
        u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        u.GetShellWindow.restype = wintypes.HWND
        u.GetDesktopWindow.restype = wintypes.HWND
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetTickCount.restype = wintypes.DWORD
        k.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        k.SetThreadExecutionState.restype = wintypes.DWORD
        k.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
        k.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD

    def idle_ms(self) -> int:
        """Milliseconds since the last input in THIS session (GetLastInputInfo vs GetTickCount, both 32-bit wrapping)."""
        info = self._LASTINPUTINFO()
        info.cbSize = self._ct.sizeof(self._LASTINPUTINFO)
        if not self._user32.GetLastInputInfo(self._ct.byref(info)):
            raise OSError("GetLastInputInfo failed")
        return (int(self._kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF

    def foreground_window(self) -> Optional[WindowInfo]:
        ct, wt, u = self._ct, self._wt, self._user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        rect = wt.RECT()
        if not u.GetWindowRect(hwnd, ct.byref(rect)):
            return None
        buf = ct.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, buf, 256)
        mon = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = self._MONITORINFO()
        mi.cbSize = ct.sizeof(self._MONITORINFO)
        if not mon or not u.GetMonitorInfoW(mon, ct.byref(mi)):
            return None
        m = mi.rcMonitor
        shell = hwnd in (u.GetShellWindow(), u.GetDesktopWindow())
        return WindowInfo(int(hwnd or 0), int(pid.value), buf.value,
                          (rect.left, rect.top, rect.right, rect.bottom), (m.left, m.top, m.right, m.bottom), bool(shell))

    def process_name(self, pid: int) -> Optional[str]:
        ct, wt, k = self._ct, self._wt, self._kernel32
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wt.DWORD(1024)
            buf = ct.create_unicode_buffer(1024)
            if not k.QueryFullProcessImageNameW(handle, 0, buf, ct.byref(size)):
                return None
            return buf.value.rsplit("\\", 1)[-1]
        finally:
            k.CloseHandle(handle)

    def notification_state(self) -> Optional[int]:
        """SHQueryUserNotificationState (QUERY_USER_NOTIFICATION_STATE as int), or None when the call fails."""
        state = self._ct.c_int(0)
        hr = self._shell32.SHQueryUserNotificationState(self._ct.byref(state))
        return int(state.value) if hr == 0 else None

    def power_status(self) -> Optional[tuple]:
        """(ACLineStatus, BatteryFlag) or None."""
        sps = self._SPS()
        if not self._kernel32.GetSystemPowerStatus(self._ct.byref(sps)):
            return None
        return int(sps.ACLineStatus), int(sps.BatteryFlag)

    def set_execution_state(self, flags: int) -> int:
        """Returns the previous state, or 0 on failure."""
        return int(self._kernel32.SetThreadExecutionState(flags))

    def active_console_session(self) -> int:
        return int(self._kernel32.WTSGetActiveConsoleSessionId())


# --------------------------------------------------------------------------- #
# Activity (runs in the user's session)
# --------------------------------------------------------------------------- #
def covers_monitor(rect: tuple, monitor: tuple) -> bool:
    """True when a window rectangle covers its whole monitor (borderless or exclusive full-screen)."""
    return rect[0] <= monitor[0] and rect[1] <= monitor[1] and rect[2] >= monitor[2] and rect[3] >= monitor[3]


class WindowsActivityProbe(ActivityProbe):
    """PAL ``ActivityProbe`` for the interactive session. Run it in the user's session, not in the service (see module doc)."""

    platform_name = "windows"

    def __init__(self, api: Any = None) -> None:
        self._api = api if api is not None else Win32Api()

    def idle_seconds(self) -> float:
        return self._api.idle_ms() / 1000.0

    def fullscreen(self) -> Optional[bool]:
        """True for exclusive or borderless full-screen apps and presentation mode; None when nothing could be read."""
        state = self._api.notification_state()
        if state in FULLSCREEN_NOTIFICATION_STATES:
            return True
        win = self._api.foreground_window()
        if win is None:
            return None if state is None else False
        if win.is_shell or win.class_name.strip().lower() in SHELL_WINDOW_CLASSES:
            return False
        return covers_monitor(win.rect, win.monitor_rect)

    def snapshot(self) -> ActivitySnapshot:
        idle = self.idle_seconds()
        fs = self.fullscreen()
        return ActivitySnapshot(owner_active=None, fullscreen_app=fs, idle_seconds=idle)

    def foreground_pid(self) -> Optional[int]:
        win = self._api.foreground_window()
        return win.pid if win is not None else None


def interactive_session_present(api: Any = None) -> bool:
    """False when no user is logged on at the console (then nobody is using the machine). RDP-only use is not seen here.
    TODO(verify-docs): WTSGetActiveConsoleSessionId returns 0xFFFFFFFF when there is no active console session."""
    api = api if api is not None else Win32Api()
    return api.active_console_session() != NO_ACTIVE_CONSOLE_SESSION


# --------------------------------------------------------------------------- #
# GPU use by the owner (NVML)
# --------------------------------------------------------------------------- #
def _nvml_gpu_pids(nvml: Any = None) -> dict:
    """pid -> used GPU memory in MB (None when the driver does not report it) for every process NVML lists.
    TODO(verify-docs): under WDDM (the Windows display driver model) NVML may list no processes or no per-process memory;
    then ``foreground_gpu_app`` stays None and the full-screen and recent-input signals carry the protection."""
    try:
        nvml = nvml or __import__("pynvml")
    except ImportError:
        raise ProbeUnsupported("nvidia-ml-py is not installed")
    out: dict = {}
    nvml.nvmlInit()
    try:
        for i in range(nvml.nvmlDeviceGetCount()):
            h = nvml.nvmlDeviceGetHandleByIndex(i)
            for fn in ("nvmlDeviceGetGraphicsRunningProcesses", "nvmlDeviceGetComputeRunningProcesses"):
                try:
                    for p in getattr(nvml, fn)(h) or []:
                        used = getattr(p, "usedGpuMemory", None)
                        out[int(p.pid)] = None if used is None or used > 2**62 else int(used) // (1024 * 1024)
                except Exception as exc:  # noqa: BLE001 - one device or call failing must not hide the others
                    logger.debug("%s failed on device %s: %s", fn, i, type(exc).__name__)
    finally:
        with contextlib.suppress(Exception):
            nvml.nvmlShutdown()
    return out


def _nvml_free_vram_mb(nvml: Any = None) -> Optional[int]:
    """The LARGEST free VRAM among GPUs (a job can run on any one of them), in MB."""
    try:
        nvml = nvml or __import__("pynvml")
    except ImportError:
        raise ProbeUnsupported("nvidia-ml-py is not installed")
    nvml.nvmlInit()
    try:
        best: Optional[int] = None
        for i in range(nvml.nvmlDeviceGetCount()):
            free = int(nvml.nvmlDeviceGetMemoryInfo(nvml.nvmlDeviceGetHandleByIndex(i)).free) // (1024 * 1024)
            best = free if best is None else max(best, free)
        return best
    finally:
        with contextlib.suppress(Exception):
            nvml.nvmlShutdown()


class WindowsOwnerGpuProbe:
    """``foreground_gpu_app()`` and ``free_vram_mb()`` for the governance engine. Run beside the activity probe."""

    def __init__(
        self,
        activity: Optional[WindowsActivityProbe] = None,
        *,
        api: Any = None,
        gpu_pids: Callable[[], dict] = _nvml_gpu_pids,
        free_vram: Callable[[], Optional[int]] = _nvml_free_vram_mb,
    ) -> None:
        self._api = api if api is not None else (activity._api if activity is not None else Win32Api())
        self._gpu_pids, self._free_vram = gpu_pids, free_vram

    def foreground_gpu_app(self) -> Optional[str]:
        win = self._api.foreground_window()
        if win is None or win.is_shell:
            return None
        if win.pid not in self._gpu_pids():
            return None
        return self._api.process_name(win.pid) or f"pid:{win.pid}"

    def free_vram_mb(self) -> Optional[int]:
        return self._free_vram()


# --------------------------------------------------------------------------- #
# Power (works from the service)
# --------------------------------------------------------------------------- #
class WinregReader:
    """Registry reads through the stdlib ``winreg``. TODO(verify-docs): KEY_WOW64_64KEY keeps a 32-bit process honest."""

    def key_exists(self, path: str) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                return True
        except FileNotFoundError:
            return False

    def value_nonempty(self, path: str, name: str) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return bool(value)
        except FileNotFoundError:
            return False


class PowerEventLatch:
    """Remembers a suspend notice until the matching resume. The SERVICE control handler calls ``handle_power_event`` with
    the ``PBT_*`` code from ``SERVICE_CONTROL_POWEREVENT``. Windows gives about two seconds after PBT_APMSUSPEND, so this
    cannot start a drain: the real protection is the sleep-prevention request while a job runs plus checkpoints."""

    def __init__(self) -> None:
        self._pending = False
        self._lock = threading.Lock()

    def handle_power_event(self, code: int) -> None:
        with self._lock:
            if code == PBT_APMSUSPEND:
                self._pending = True
            elif code in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                self._pending = False

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending


# One latch per process: the Windows Service control handler (service.py) writes to it and the yield engine reads it
# through WindowsPowerState, which uses it unless a test injects its own.
_SHARED_POWER_LATCH = PowerEventLatch()


def shared_power_latch() -> PowerEventLatch:
    return _SHARED_POWER_LATCH


class WindowsPowerState(PowerState):
    """PAL ``PowerState``: battery, reboot-pending (registry), and the suspend latch."""

    platform_name = "windows"

    def __init__(
        self,
        api: Any = None,
        registry: Any = None,
        latch: Optional[PowerEventLatch] = None,
        *,
        include_pending_file_rename: bool = False,
    ) -> None:
        self._api = api if api is not None else Win32Api()
        self._registry = registry if registry is not None else WinregReader()
        self.latch = latch if latch is not None else shared_power_latch()
        self._include_rename = include_pending_file_rename

    def on_battery(self) -> Optional[bool]:
        status = self._api.power_status()
        if status is None:
            return None
        ac, flag = status
        if flag == BATTERY_FLAG_NO_BATTERY:
            return False  # a desktop with no battery cannot be "on battery"
        if ac == AC_LINE_OFFLINE:
            return True
        if ac == AC_LINE_ONLINE:
            return False
        return None  # AC_LINE_UNKNOWN: unknown, the policy fails closed

    def reboot_pending(self) -> Optional[bool]:
        try:
            if any(self._registry.key_exists(k) for k in REBOOT_KEYS):
                return True
            if self._include_rename and self._registry.value_nonempty(PENDING_RENAME_KEY, PENDING_RENAME_VALUE):
                return True
            return False
        except Exception as exc:  # noqa: BLE001 - unreadable registry means unknown, never "no reboot pending"
            logger.debug("reboot-pending check failed: %s", type(exc).__name__)
            return None

    def snapshot(self) -> PowerSnapshot:
        return PowerSnapshot(
            on_battery=self.on_battery(),
            sleep_pending=self.latch.pending,
            reboot_pending=self.reboot_pending(),
        )


# --------------------------------------------------------------------------- #
# Sleep prevention (only while a job runs)
# --------------------------------------------------------------------------- #
class SleepInhibitor:
    """Asks Windows not to sleep the SYSTEM while a job runs; asks for nothing the rest of the time.

    ``SetThreadExecutionState`` with ``ES_CONTINUOUS`` is per THREAD and lasts until that thread clears it or exits, so one
    dedicated thread owns the request. The display may still turn off. A user pressing Sleep, closing a laptop lid or a
    forced policy still suspends the machine: this only removes the idle timeout. TODO(verify-docs): for a service,
    ``PowerCreateRequest`` / ``PowerSetRequest`` is Microsoft's recommended alternative; decide with the supervisor owner.
    """

    def __init__(self, api: Any = None) -> None:
        self._api = api if api is not None else Win32Api()
        self._lock = threading.Lock()
        self._count = 0
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self.held = False
        self._synced = False

    def acquire(self) -> None:
        with self._lock:
            self._count += 1
            if self._thread is not None:
                return
            ready, self._stop = threading.Event(), threading.Event()
            stop = self._stop

            def run() -> None:
                ok = self._api.set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0
                self.held = ok
                ready.set()
                if not ok:
                    logger.warning("could not request that the system stay awake")
                    return
                stop.wait()
                self._api.set_execution_state(ES_CONTINUOUS)  # clear: back to the normal idle timeout
                self.held = False

            self._thread = threading.Thread(target=run, name="computecoin-sleep-inhibit", daemon=True)
            self._thread.start()
            ready.wait(5.0)

    def release(self) -> None:
        with self._lock:
            if self._count == 0:
                return
            self._count -= 1
            if self._count > 0 or self._thread is None:
                return
            stop, thread = self._stop, self._thread
            self._stop, self._thread = None, None
        assert stop is not None
        stop.set()
        thread.join(5.0)

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def sync(self, wanted: bool) -> None:
        """Follow ``Decision.prevent_sleep``: hold while it is True, drop it when it turns False. Idempotent."""
        with self._lock:
            holding = self._synced
            self._synced = wanted
        if wanted and not holding:
            self.acquire()
        elif not wanted and holding:
            self.release()


# --------------------------------------------------------------------------- #
# Session helper entry point
# --------------------------------------------------------------------------- #
def take_snapshot(activity: WindowsActivityProbe, owner_gpu: WindowsOwnerGpuProbe, *, clock: Callable[[], float] = time.time) -> str:
    """One JSON line for ``snapshot.SnapshotProbe``. A signal that cannot be read is null (unknown), never guessed."""
    def safe(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("snapshot probe failed: %s", type(exc).__name__)
            return None

    return encode_snapshot(
        taken_at=clock(),
        fullscreen=safe(activity.fullscreen),
        idle_seconds=safe(activity.idle_seconds),
        foreground_gpu_app=safe(owner_gpu.foreground_gpu_app),
        free_vram_mb=safe(owner_gpu.free_vram_mb),
    )


def read_key_file(path: "str | Path") -> bytes:
    """The helper's copy of the per-install snapshot key (64 hex characters). Strict: a missing or odd file is an error,
    never "no key"."""
    from .snapshot import SnapshotError, key_bytes

    try:
        with open(path, "r", encoding="ascii") as fh:
            return key_bytes(fh.read(256))
    except (OSError, UnicodeDecodeError) as exc:
        raise SnapshotError(f"cannot read the snapshot key file ({type(exc).__name__})") from None


def run_snapshot_loop(
    activity: Any,
    owner_gpu: Any,
    *,
    out_path: "Optional[str | Path]" = None,
    key: Optional[bytes] = None,
    interval: float = 5.0,
    once: bool = False,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = lambda line: print(line, flush=True),
) -> int:
    """The helper's loop. With ``out_path`` and ``key`` each snapshot is written as an HMAC-signed file (the supervisor
    verifies it, ``snapshot.SignedSnapshotSource``); without them the plain line goes to ``emit`` (stdout), which is
    only meant for a person debugging. A write failure is logged and retried: the supervisor treats a stale file as
    "block", so a failing helper never opens the gate."""
    from .snapshot import write_signed_snapshot

    if (out_path is None) != (key is None):
        raise ValueError("out_path and key go together")
    while True:
        line = take_snapshot(activity, owner_gpu, clock=clock)
        if out_path is not None and key is not None:
            try:
                write_signed_snapshot(out_path, key, line)
            except OSError as exc:
                logger.warning("could not write the snapshot file: %s", type(exc).__name__)
        else:
            emit(line)
        if once:
            return 0
        sleeper(max(1.0, interval))


def main(argv: Optional[list] = None) -> int:
    """``python -m computecoin_agent.windows.governance.probes --snapshot-json [--interval 5] [--out FILE --key-file FILE]``.

    Without ``--out`` it prints one JSON snapshot per interval on stdout (debugging). With ``--out`` and ``--key-file``
    (what ``service helper run`` uses) it writes the signed snapshot file the supervisor verifies. Reads only; changes
    nothing on the machine apart from that one file."""
    parser = argparse.ArgumentParser(prog="computecoin-governance-probe")
    parser.add_argument("--snapshot-json", action="store_true", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--out", help="write the HMAC-signed snapshot file here (needs --key-file)")
    parser.add_argument("--key-file", help="file holding the per-install snapshot key (64 hex characters)")
    args = parser.parse_args(argv)
    if bool(args.out) != bool(args.key_file):
        parser.error("--out and --key-file must be given together")
    key = read_key_file(args.key_file) if args.key_file else None
    api = Win32Api()
    activity = WindowsActivityProbe(api)
    owner_gpu = WindowsOwnerGpuProbe(activity)
    return run_snapshot_loop(activity, owner_gpu, out_path=args.out, key=key, interval=args.interval, once=args.once)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
