"""``computecoin-agent rental check``: prove this computer can host a container rental BEFORE a customer finds out it cannot.

It runs the same kind of container a rental uses (read-only root, no capabilities, no network, the customer's unprivileged user,
the same kind of workspace folder) with the GPU attached, and says in plain words what works and what does not. It changes nothing
on this computer except a short-lived folder it deletes again, and one throw-away container that is removed when it exits.

Every external command goes through ``runner`` (``argv, timeout -> object with returncode, stdout, stderr``) so tests run with no
Docker at all.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .platform import PLATFORM_WINDOWS, get_platform
from .rental import FREE_DISK_MARGIN, GIB, RentalError, Workspace, assert_rental_argv_safe

# A small public image with a shell and nvidia-smi support, used only for this check. Override with --image (for example the template
# image a host intends to serve) to also prove that image can be pulled.
CHECK_IMAGE = "nvidia/cuda:12.4.1-base-ubuntu22.04"
CHECK_TIMEOUT_SECONDS = 900  # the first run pulls the image

# The setup page asks for NVIDIA driver 535.86 or newer, so the check must not fail a host on 535 to 549 with an image that needs 550.
# It picks the newest CUDA base image the host's driver can run (driver floors: CUDA 12.4 needs 550, CUDA 12.2 needs 535). An unreadable
# driver version falls back to the 535 image, the one that matches what the page asks for. `--image` always wins.
MIN_DRIVER_MAJOR = 535
CHECK_IMAGES = ((550, CHECK_IMAGE), (535, "nvidia/cuda:12.2.2-base-ubuntu22.04"))
FALLBACK_CHECK_IMAGE = "nvidia/cuda:12.2.2-base-ubuntu22.04"


def driver_major(text: Optional[str]) -> Optional[int]:
    """The major number of an NVIDIA driver version such as ``550.120`` (first line only), or None when it is not one."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    m = re.match(r"^\s*(\d{3,4})\.\d+", first)
    return int(m.group(1)) if m else None


def pick_check_image(driver_text: Optional[str]) -> str:
    major = driver_major(driver_text)
    if major is None:
        return FALLBACK_CHECK_IMAGE
    for floor, image in CHECK_IMAGES:
        if major >= floor:
            return image
    return FALLBACK_CHECK_IMAGE  # below every floor: the "driver" check fails first and says what to do


def driver_check(driver_text: Optional[str]) -> Optional["Check"]:
    """The driver line of the report, or None when the version could not be read (the report then says nothing rather than guess)."""
    major = driver_major(driver_text)
    if major is None:
        return None
    version = (driver_text or "").strip().splitlines()[0].strip()
    if major < MIN_DRIVER_MAJOR:
        return Check("NVIDIA driver", FAIL, f"Driver {version} is older than the {MIN_DRIVER_MAJOR} that container rental needs.",
                     "Update the NVIDIA driver (535.86 or newer), restart the computer and run this check again.")
    return Check("NVIDIA driver", PASS, f"Driver {version}.")
OK_MARK = "CHECK-OK"

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str  # pass | warn | fail
    detail: str
    fix: str = ""


Runner = Callable[[list, float], Any]


def default_runner(argv: list, timeout: float) -> Any:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _run(runner: Runner, argv: list, timeout: float = 60) -> Any:
    try:
        return runner(argv, timeout)
    except subprocess.TimeoutExpired:
        class _T:
            returncode, stdout, stderr = 124, "", "timed out"

        return _T()
    except OSError as exc:
        class _E:
            returncode, stdout, stderr = 127, "", type(exc).__name__

        return _E()


def _out(res: Any) -> str:
    return str(getattr(res, "stdout", "") or "").strip()


def _err(res: Any) -> str:
    return str(getattr(res, "stderr", "") or "").strip()[:300]


def run_checks(
    *,
    allowed: bool,
    root: str,
    image: Optional[str] = None,
    runner: Runner = default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    egress_proxy_image: Optional[str] = None,
    free_bytes: Optional[Callable[[str], int]] = None,
) -> list[Check]:
    """All the checks, in order. Never raises."""
    chosen_image = image  # an image the host named is always used as given
    image = image or CHECK_IMAGE
    out: list[Check] = []
    free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)

    # 1. the platform
    if get_platform().name == PLATFORM_WINDOWS:
        out.append(Check("Operating system", FAIL, "Container rental is not available on Windows.", "Use a Linux computer."))
        return out
    out.append(Check("Operating system", PASS, "Linux."))

    # 2. the setting
    if allowed:
        out.append(Check("Container rental setting", PASS, "Turned on."))
    else:
        out.append(Check("Container rental setting", WARN, "Turned off, so customers cannot rent this computer yet.",
                         "When the checks below pass, run: computecoin-agent rental on"))

    # 3. docker
    if not which("docker"):
        out.append(Check("Docker", FAIL, "Docker is not installed.", "Install Docker Engine, then run this check again."))
        return out
    ver = _run(runner, ["docker", "version", "--format", "{{.Server.Version}}"])
    if getattr(ver, "returncode", 1) != 0 or not _out(ver):
        out.append(Check("Docker", FAIL, "Docker is installed but this user cannot reach it, or it is not running.",
                         "Start it (sudo systemctl start docker) and make sure the agent's user is in the docker group."))
        return out
    out.append(Check("Docker", PASS, f"Docker {_out(ver)} is running."))

    # 4. the graphics cards
    gpus: list[str] = []
    if which("nvidia-smi"):
        res = _run(runner, ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
        if getattr(res, "returncode", 1) == 0:
            gpus = [ln.strip() for ln in _out(res).splitlines() if ln.strip()]
    if gpus:
        out.append(Check("Graphics card", PASS, f"{len(gpus)} found: " + "; ".join(gpus[:4]) + (" ..." if len(gpus) > 4 else "")))
        drv = _run(runner, ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
        driver_text = _out(drv) if getattr(drv, "returncode", 1) == 0 else None
        line = driver_check(driver_text)
        if line is not None:
            out.append(line)
        if chosen_image is None:
            image = pick_check_image(driver_text)
    else:
        out.append(Check("Graphics card", FAIL, "No NVIDIA graphics card answered (nvidia-smi).",
                         "Install the NVIDIA driver so that nvidia-smi lists your card, then restart the computer. If you just installed it and Secure "
                         "Boot is on, the driver may be blocked: turn Secure Boot off or sign the driver. AMD cards are not supported for rentals."))

    # 5. disk
    root_parent = root if os.path.isdir(root) else (os.path.dirname(root.rstrip(os.sep)) or ".")
    try:
        free = int(free_bytes(root_parent))
    except OSError:
        free = 0
    if free < FREE_DISK_MARGIN * 2:
        out.append(Check("Disk space", FAIL, f"Only {free / GIB:.1f} GB free where rental workspaces are kept ({root}).",
                         "Free some space or set rental_root in the agent settings to a bigger disk."))
    else:
        out.append(Check("Disk space", PASS, f"{free / GIB:.0f} GB free where rental workspaces are kept."))

    # 6. network filter (needed by any rental that asks for package sites, including both templates)
    if egress_proxy_image and "@sha256:" in egress_proxy_image:
        out.append(Check("Network filter", PASS, "The filtering proxy for allowed package sites is configured."))
    else:
        out.append(Check("Network filter", WARN,
                         "No filtering proxy is configured, so this computer will only take rentals that need NO internet "
                         "(the ready-made templates ask for package sites, so customers cannot use them here).",
                         "Set egress_proxy_image (pinned by @sha256: digest) in the agent settings."))

    # 7. the real test: a hardened container, with the GPU, writing to a workspace folder like a rental's
    sid = str(uuid.uuid4())
    ws: Optional[Workspace] = None
    try:
        ws = Workspace(root, sid, GIB)
        ws.create()
    except (RentalError, OSError) as exc:
        out.append(Check("Workspace folder", FAIL, f"Could not create a rental workspace folder ({exc}).",
                         "Check that the agent's user can write to the rental folder."))
        return out
    try:
        argv = ["docker", "run", "--rm", "--name", f"ccoin-check-{sid}", "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "65534:65534", "--pids-limit", "256", "--memory", "1024m",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m"]
        if gpus:
            argv += ["--gpus", "1"]
        argv += ["--mount", f"type=bind,source={ws.path},target=/workspace", "--workdir", "/workspace", image,
                 "sh", "-c", (f"{'nvidia-smi -L && ' if gpus else ''}touch /workspace/.check && echo {OK_MARK}")]
        try:
            assert_rental_argv_safe(argv, [ws.path])
        except RentalError as exc:  # a bug in this check, not the host: say so rather than blame the computer
            out.append(Check("Test container", FAIL, f"The check itself is wrong ({exc}). Please report this.", ""))
            return out
        res = _run(runner, argv, CHECK_TIMEOUT_SECONDS)
        text = _out(res)
        ran_ok = getattr(res, "returncode", 1) == 0 and OK_MARK in text
        if ran_ok:
            made = os.path.exists(os.path.join(ws.path, ".check"))
            detail = "A locked-down container started" + (", saw the graphics card" if gpus else "") + " and wrote to its workspace."
            out.append(Check("Test container", PASS if made else WARN, detail if made else detail + " (but its file was not found in the folder)."))
        else:
            reason, fix = _explain_failure(_err(res) + " " + text, gpus=bool(gpus), image=image)
            out.append(Check("Test container", FAIL, reason, fix))
    finally:
        ws.delete()
        if os.path.exists(ws.top):  # files the container made as another user: the agent has a helper for this; just say so
            out.append(Check("Cleanup", WARN, "The test folder could not be fully removed by this user (the agent has a fallback for this).",
                             f"You may delete it yourself: {ws.top}"))
    return out


def _explain_failure(text: str, *, gpus: bool, image: str) -> tuple[str, str]:
    t = text.lower()
    if "unsatisfied condition" in t or "cuda>=" in t or "driver version is insufficient" in t or "requirement error" in t:
        return ("The graphics-card driver is too old for the test image.",
                "Update the NVIDIA driver (535.86 or newer), restart the computer and run this check again.")
    if "failed to initialize nvml" in t:
        return ("The graphics card stopped answering inside Docker containers (a known Docker issue after Docker or the system reloads).",
                "Restart Docker (sudo systemctl restart docker) and run this check again. If it keeps coming back, see the NVIDIA Container "
                "Toolkit troubleshooting page for 'Failed to initialize NVML: Unknown Error'.")
    if "could not select device driver" in t or ("nvidia" in t and "runtime" in t) or ("gpu" in t and "driver" in t):
        return ("Docker cannot give a container the graphics card.",
                "Install the NVIDIA Container Toolkit and register it with Docker (sudo nvidia-ctk runtime configure --runtime=docker), "
                "then restart Docker.")
    if "pull access denied" in t or "manifest unknown" in t or "no such image" in t or "repository does not exist" in t:
        return (f"The test image {image} could not be pulled.", "Check this computer's internet connection, or give another public image with --image.")
    if "permission denied" in t and ("docker.sock" in t or "daemon" in t):
        return ("This user is not allowed to use Docker.", "Add the agent's user to the docker group and sign in again.")
    if "timed out" in t:
        return ("The test container took too long (the first pull of a big image can be slow).", "Run the check again.")
    if "read-only file system" in t or "permission denied" in t:
        return ("The container could not write to its workspace folder.", "Check the permissions of the rental folder and that it is on an ordinary disk.")
    if not gpus:
        return ("The test container did not start.", "Run the check again, or read Docker's own message with: docker run --rm hello-world")
    return ("The test container did not run cleanly with the graphics card.",
            "Check that `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` works on this computer.")


def summarize(checks: list[Check]) -> tuple[int, str]:
    """``(exit code, text)``: 0 when nothing failed (warnings are allowed), 1 otherwise."""
    marks = {PASS: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}
    lines = []
    for c in checks:
        lines.append(f"{marks[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != PASS:
            lines.append(f"       What to do: {c.fix}")
    failed = [c for c in checks if c.status == FAIL]
    lines.append("")
    if failed:
        lines.append("This computer is NOT ready to host container rentals. Fix the items marked FAIL, then run the check again.")
    else:
        lines.append("This computer passed the check." + (" Warnings above limit what customers can do here." if any(c.status == WARN for c in checks) else ""))
    return (1 if failed else 0), "\n".join(lines)
