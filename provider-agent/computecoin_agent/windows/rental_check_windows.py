"""``computecoin-agent rental check`` on a Windows host (EXPERIMENTAL).

On Windows the customer's container runs inside the managed Linux system (WSL2, distro ``computecoin``), so every check runs THERE
through ``wsl.exe``, as the runner's own user. It proves what the Linux check proves (Docker answers, a graphics card is visible, there
is disk, a locked-down container runs and writes to a workspace folder like a rental's) and two things that matter only here:

* the Windows ``C:`` drive must NOT be visible inside the Linux system (a customer who escaped a container would otherwise read the
  owner's Windows files);
* Windows programs must not be launchable from inside it (interop off).

The check changes nothing except one short-lived folder inside the Linux system, removed again, and one throw-away container.
Every command is an argument list (no shell between ``wsl.exe`` and the program). Pure of Windows: the runner is injected, so tests run
anywhere with a scripted ``wsl.exe``.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from ..rental import FREE_DISK_MARGIN, GIB, RentalError, assert_rental_argv_safe
from ..rental_check import CHECK_IMAGE, CHECK_TIMEOUT_SECONDS, FAIL, OK_MARK, PASS, WARN, Check, _explain_failure, driver_check, pick_check_image
from .runtime import distro_paths as P
from .runtime.wsl import DEFAULT_WSL_EXE, RunResult, WslCli, WslError

SCRATCH_PREFIX = "/var/tmp/ccoin-check-"  # inside the Linux system; the only place this check writes
WINDOWS_DRIVE = "/mnt/c"
INTEROP_FILE = "/proc/sys/fs/binfmt_misc/WSLInterop"
WSL2_VERSION = 2
# Inside a WSL2 system the card's tools live here. A program started directly (no login shell) may not have this folder on its path, so
# `nvidia-smi` is tried by name first and by this full path second.
WSL_SMI = "/usr/lib/wsl/lib/nvidia-smi"


def _text(res: RunResult) -> str:
    return (res.stdout or "").strip()


def _err(res: RunResult) -> str:
    return (res.stderr or "").strip()[:300]


def run_windows_checks(
    *,
    rental_on: bool,
    windows_switch_on: bool,
    wsl: WslCli,
    image: Optional[str] = None,
    egress_proxy_image: Optional[str] = None,
) -> list[Check]:
    """All the checks, in order. Never raises."""
    chosen_image = image  # an image the host named is always used as given
    image = image or CHECK_IMAGE
    out: list[Check] = []
    user = P.RUNNER_USER

    def inside(program: list[str], *, who: str = user, timeout: float = 60) -> Optional[RunResult]:
        try:
            return wsl.exec(program, user=who, timeout=timeout)
        except (WslError, OSError) as exc:
            return RunResult(127, "", type(exc).__name__)

    # 1. the platform
    out.append(Check("Operating system", PASS, "Windows. Customers' containers run inside the ComputeCoin Linux system (WSL2). Experimental."))

    # 2. the settings (both must be on)
    if rental_on and windows_switch_on:
        out.append(Check("Container rental setting", PASS, "Turned on."))
    elif rental_on or windows_switch_on:
        out.append(Check("Container rental setting", WARN, "Only half turned on, so customers cannot rent this computer yet.",
                         "Run: computecoin-agent rental on"))
    else:
        out.append(Check("Container rental setting", WARN, "Turned off, so customers cannot rent this computer yet.",
                         "When the checks below pass, run: computecoin-agent rental on"))

    # 3. WSL and the managed Linux system
    try:
        version = wsl.version()
        info = wsl.distro_info() if version is not None else None
    except (WslError, OSError):
        version, info = None, None
    if version is None:
        out.append(Check("Windows Subsystem for Linux", FAIL, "WSL is not installed, or it did not answer.",
                         "Install or update WSL (in an administrator PowerShell: wsl --update), restart Windows, then run this check again."))
        return out
    if info is None:
        out.append(Check("ComputeCoin Linux system", FAIL, f"The Linux system '{P.DISTRO_NAME}' is not installed.",
                         "Run the ComputeCoin Windows setup that installs it, then run this check again."))
        return out
    if getattr(info, "version", None) != WSL2_VERSION:
        out.append(Check("ComputeCoin Linux system", FAIL, f"The Linux system '{P.DISTRO_NAME}' is not running as WSL 2.",
                         f"Convert it: wsl --set-version {P.DISTRO_NAME} 2"))
        return out
    out.append(Check("ComputeCoin Linux system", PASS, f"'{P.DISTRO_NAME}' is installed and runs as WSL 2."))

    # 4. docker inside it
    ver = inside(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    if ver is None or not ver.ok or not _text(ver):
        out.append(Check("Docker", FAIL, "Docker inside the ComputeCoin Linux system did not answer.",
                         "Run the ComputeCoin Windows setup again to repair it, then run this check again."))
        return out
    out.append(Check("Docker", PASS, f"Docker {_text(ver)} is running inside the Linux system."))

    # 5. the graphics card, as the Linux system sees it (it uses the Windows NVIDIA driver)
    def smi(args: list[str]) -> Optional[RunResult]:
        found = inside(["nvidia-smi", *args])
        if found is None or not found.ok:
            alt = inside([WSL_SMI, *args])
            if alt is not None and alt.ok:
                return alt
        return found

    gpus: list[str] = []
    res = smi(["--query-gpu=name,memory.total", "--format=csv,noheader"])
    if res is not None and res.ok:
        gpus = [ln.strip() for ln in _text(res).splitlines() if ln.strip()]
    if gpus:
        out.append(Check("Graphics card", PASS, f"{len(gpus)} found: " + "; ".join(gpus[:4]) + (" ..." if len(gpus) > 4 else "")))
        drv = smi(["--query-gpu=driver_version", "--format=csv,noheader"])
        driver_text = _text(drv) if drv is not None and drv.ok else None
        line = driver_check(driver_text)
        if line is not None:
            out.append(line)
        if chosen_image is None:
            image = pick_check_image(driver_text)
    else:
        out.append(Check("Graphics card", FAIL, "No NVIDIA graphics card answered inside the Linux system.",
                         "Install the current NVIDIA driver on Windows (the Linux system uses it), restart Windows, and run this check again. "
                         "AMD cards are not supported for rentals."))

    # 6. the Windows drive must not be visible to customer code
    drive = inside(["ls", "-A", WINDOWS_DRIVE], who="root")
    if drive is not None and drive.ok and _text(drive):
        out.append(Check("Windows drive hidden", FAIL, "Your Windows C: drive is visible inside the Linux system, so a customer who broke out "
                                                      "of a container could read your Windows files.",
                         "Turn off automatic drive mounting: in the Linux system's /etc/wsl.conf set [automount] enabled=false, then run "
                         "'wsl --shutdown' and the check again."))
    else:
        out.append(Check("Windows drive hidden", PASS, "The Windows C: drive is not visible inside the Linux system."))

    # 7. Windows programs must not start from inside
    interop = inside(["test", "-e", INTEROP_FILE], who="root")
    if interop is not None and interop.ok:
        out.append(Check("Windows programs blocked", WARN, "Programs inside the Linux system can start Windows programs.",
                         "Turn it off: in /etc/wsl.conf set [interop] enabled=false, then run 'wsl --shutdown' and the check again."))
    else:
        out.append(Check("Windows programs blocked", PASS, "Programs inside the Linux system cannot start Windows programs."))

    # 8. disk inside the Linux system (where Docker and the workspaces live)
    free = 0
    df = inside(["df", "-Pk", "/"], who="root")
    if df is not None and df.ok:
        rows = [ln.split() for ln in _text(df).splitlines()[1:] if ln.strip()]
        try:
            free = int(rows[-1][3]) * 1024
        except (IndexError, ValueError):
            free = 0
    if free < FREE_DISK_MARGIN * 2:
        out.append(Check("Disk space", FAIL, f"Only {free / GIB:.1f} GB free inside the Linux system, where rental workspaces are kept.",
                         "Free space on the Windows drive that holds the Linux system, or move it to a bigger drive."))
    else:
        out.append(Check("Disk space", PASS, f"{free / GIB:.0f} GB free inside the Linux system, where rental workspaces are kept."))

    # 9. network filter
    if egress_proxy_image and "@sha256:" in egress_proxy_image:
        out.append(Check("Network filter", PASS, "The filtering proxy for allowed package sites is configured."))
    else:
        out.append(Check("Network filter", WARN,
                         "No filtering proxy is configured, so this computer will only take rentals that need NO internet.",
                         "Set egress_proxy_image (pinned by @sha256: digest) in the agent settings."))

    # 10. the real test: a locked-down container, with the card, writing to a workspace folder inside the Linux system
    scratch = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    made = inside(["mkdir", "-m", "777", scratch], who="root")
    if made is None or not made.ok:
        out.append(Check("Workspace folder", FAIL, "Could not create a test workspace folder inside the Linux system.",
                         "Run the ComputeCoin Windows setup again to repair the Linux system."))
        return out
    try:
        argv = ["docker", "run", "--rm", "--name", f"ccoin-check-{uuid.uuid4().hex[:12]}", "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "65534:65534", "--pids-limit", "256", "--memory", "1024m",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m"]
        if gpus:
            argv += ["--gpus", "all"]  # in WSL 2 Docker supports only `--gpus all` (NVIDIA's CUDA-on-WSL guide): the test container sees every card
        argv += ["--mount", f"type=bind,source={scratch},target=/workspace", "--workdir", "/workspace", image,
                 "sh", "-c", (f"{'nvidia-smi -L && ' if gpus else ''}touch /workspace/.check && echo {OK_MARK}")]
        try:
            assert_rental_argv_safe(argv, [scratch])
        except RentalError as exc:  # a bug in this check, not the computer
            out.append(Check("Test container", FAIL, f"The check itself is wrong ({exc}). Please report this.", ""))
            return out
        ran = inside(argv, timeout=CHECK_TIMEOUT_SECONDS)
        text = _text(ran) if ran is not None else ""
        if ran is not None and ran.ok and OK_MARK in text:
            wrote = inside(["test", "-e", f"{scratch}/.check"], who="root")
            detail = "A locked-down container started" + (", saw the graphics card" if gpus else "") + " and wrote to its workspace."
            made_file = wrote is not None and wrote.ok
            out.append(Check("Test container", PASS if made_file else WARN, detail if made_file else detail + " (but its file was not found in the folder)."))
        else:
            reason, fix = _explain_failure((_err(ran) if ran is not None else "") + " " + text, gpus=bool(gpus), image=image)
            out.append(Check("Test container", FAIL, reason, fix))
    finally:
        if scratch.startswith(SCRATCH_PREFIX) and ".." not in scratch:  # only ever remove the folder this check made
            gone = inside(["rm", "-rf", "--", scratch], who="root")
            if gone is None or not gone.ok:
                out.append(Check("Cleanup", WARN, "The test folder could not be removed.", f"You can delete it inside the Linux system: {scratch}"))
    return out


def default_wsl(runner: Optional[Callable[..., Any]] = None) -> WslCli:
    """The real ``wsl.exe`` wrapper for the managed Linux system (runs only on Windows)."""
    if runner is None:
        from .runtime.wsl import subprocess_runner as runner  # type: ignore[assignment]
    return WslCli(runner, P.DISTRO_NAME, wsl_exe=DEFAULT_WSL_EXE, default_timeout=600.0)  # type: ignore[arg-type]
