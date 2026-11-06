"""Runtime health checks and the GPU smoke test (W3.2, W3.4).

Every check returns a :class:`Check` and never raises: an exception inside a check is a FAILED check (L1). The checks
read the live distro through ``WslCli``; they do not trust the provisioning state file.

TODO(verify-docs) (X11): every guest path and command below (``/dev/dxg``, ``/usr/lib/wsl/lib``, the binfmt interop
entries, ``ss``, ``nvidia-ctk``, ``docker version --format``) is an assumption about a current WSL2 + systemd + Ubuntu
distro and was never run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import distro_paths as P
from .config_files import hardening_problems
from .manifest import RuntimeManifest
from .wsl import WslCli

WINDOWS_BACKED_FSTYPES = frozenset({"9p", "drvfs", "virtiofs"})
_GPU_LINE = re.compile(r"^GPU (\d+): (.+?) \(UUID: (GPU-[0-9A-Fa-f-]+)\)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class HealthReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks]}


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # a check that cannot run is a failed check
        return Check(name, False, f"check could not run: {type(exc).__name__}: {str(exc)[:200]}")


def windows_backed_mounts(proc_mounts: str) -> list[str]:
    """Mount points of Windows-backed file systems that are NOT on the allow list (any one of them is an X6 leak)."""
    leaks = []
    for line in proc_mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in WINDOWS_BACKED_FSTYPES and parts[1] not in P.ALLOWED_WINDOWS_BACKED_MOUNTS:
            leaks.append(parts[1])
    return leaks


def non_loopback_listeners(ss_output: str) -> list[str]:
    """Listening TCP sockets whose local address is not loopback, from ``ss -H -ltn``."""
    out = []
    for line in ss_output.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        host = local.rsplit(":", 1)[0].split("%", 1)[0].strip("[]")
        if not (host.startswith("127.") or host == "::1"):
            out.append(local)
    return out


def parse_gpu_list(nvidia_smi_l: str) -> list[tuple[int, str, str]]:
    return [(int(m.group(1)), m.group(2), m.group(3)) for m in _GPU_LINE.finditer(nvidia_smi_l or "")]


def run_health(cli: WslCli, manifest: Optional[RuntimeManifest], *, runner_user: str = P.RUNNER_USER) -> HealthReport:
    rep = HealthReport()

    def add(name: str, fn: Callable[[], Check]) -> None:
        rep.checks.append(_guard(name, fn))

    def distro() -> Check:
        info = cli.distro_info()
        if info is None:
            return Check("distro_registered", False, f"distro {cli.distro!r} is not registered")
        return Check("distro_registered", info.version == 2, f"WSL version {info.version}, state {info.state}")

    def wsl_conf() -> Check:
        problems = hardening_problems(cli.read_file(P.WSL_CONF))
        return Check("wsl_conf_hardened", not problems, "; ".join(problems) or "automount, interop, PATH off; systemd on")

    def systemd() -> Check:
        res = cli.exec(["cat", P.GUEST_PID1_COMM], timeout=30)
        return Check("systemd_is_pid1", res.ok and res.stdout.strip() == "systemd", res.stdout.strip() or res.stderr.strip())

    def no_windows_mounts() -> Check:
        res = cli.exec(["cat", P.GUEST_MOUNTS], timeout=30)
        if not res.ok:
            return Check("no_windows_mounts", False, "cannot read the mount table")
        leaks = windows_backed_mounts(res.stdout)
        return Check("no_windows_mounts", not leaks, ("Windows-backed mounts: " + ", ".join(leaks)) if leaks else "none")

    def no_interop() -> Check:
        present = [p for p in P.GUEST_BINFMT_INTEROP if cli.exec(["test", "-e", p], timeout=30).ok]
        return Check("interop_disabled", not present, ("interop registered: " + ", ".join(present)) if present else "no WSLInterop entry")

    def no_listeners() -> Check:
        res = cli.exec(["ss", "-H", "-ltn"], timeout=30)
        if not res.ok:
            return Check("no_network_listeners", False, "cannot list listening sockets (ss missing?)")
        bad = non_loopback_listeners(res.stdout)
        return Check("no_network_listeners", not bad, ("listening on: " + ", ".join(bad)) if bad else "loopback only")

    def ssh_off() -> Check:
        res = cli.exec(["systemctl", "is-enabled", "ssh.service", "ssh.socket", "sshd.service"], timeout=30)
        states = [s.strip() for s in res.stdout.splitlines() if s.strip()]
        bad = [s for s in states if s in ("enabled", "static", "alias", "indirect", "generated", "linked")]
        active = cli.exec(["systemctl", "is-active", "ssh.service", "ssh.socket", "sshd.service"], timeout=30)
        running = [s for s in active.stdout.split() if s == "active"]
        return Check("ssh_disabled", not bad and not running, f"is-enabled={states} active={len(running)}")

    def user() -> Check:
        uid = cli.exec(["id", "-u", runner_user], timeout=30)
        if not uid.ok or not uid.stdout.strip().isdigit():
            return Check("runner_user", False, f"user {runner_user!r} does not exist")
        if int(uid.stdout.strip()) == 0:
            return Check("runner_user", False, "runner user is uid 0")
        groups = cli.exec(["id", "-nG", runner_user], timeout=30).stdout.split()
        privileged = [g for g in groups if g in ("sudo", "root", "admin", "wheel")]
        return Check("runner_user", not privileged, f"uid {uid.stdout.strip()}; privileged groups: {privileged or 'none'}")

    def docker() -> Check:
        res = cli.exec(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60)
        have = res.stdout.strip()
        if not res.ok or not have:
            return Check("docker_engine", False, "docker daemon not answering")
        if manifest and not have.startswith(manifest.docker_engine_version):
            return Check("docker_engine", False, f"engine {have}, manifest pins {manifest.docker_engine_version}")
        return Check("docker_engine", True, f"engine {have}")

    def toolkit() -> Check:
        res = cli.exec(["nvidia-ctk", "--version"], timeout=30)
        if not res.ok:
            return Check("nvidia_toolkit", False, "nvidia-ctk not found")
        if manifest and manifest.nvidia_toolkit_version not in res.stdout:
            return Check("nvidia_toolkit", False, f"toolkit differs from pinned {manifest.nvidia_toolkit_version}")
        rt = cli.exec(["docker", "info", "--format", "{{json .Runtimes}}"], timeout=60)
        if not rt.ok or "nvidia" not in rt.stdout:
            return Check("nvidia_toolkit", False, "docker has no nvidia runtime registered")
        return Check("nvidia_toolkit", True, "installed, docker runtime registered")

    def runner() -> Check:
        res = cli.exec([f"{P.VENV_DIR}/bin/python", "-m", "pip", "show", "computecoin-agent"], timeout=60)
        m = re.search(r"^Version:\s*(\S+)", res.stdout, re.MULTILINE)
        if not res.ok or not m:
            return Check("runner_installed", False, "runner package not installed in the venv")
        if manifest and m.group(1) != manifest.runner_version:
            return Check("runner_installed", False, f"runner {m.group(1)}, manifest pins {manifest.runner_version}")
        return Check("runner_installed", True, f"runner {m.group(1)}")

    def gpu_paths() -> Check:
        dev = cli.exec(["test", "-e", P.GUEST_GPU_DEVICE], timeout=30).ok
        lib = cli.exec(["test", "-d", P.GUEST_GPU_LIB_DIR], timeout=30).ok
        return Check("gpu_passthrough_present", dev and lib, f"{P.GUEST_GPU_DEVICE}: {dev}, {P.GUEST_GPU_LIB_DIR}: {lib}")

    for name, fn in (
        ("distro_registered", distro), ("wsl_conf_hardened", wsl_conf), ("systemd_is_pid1", systemd),
        ("no_windows_mounts", no_windows_mounts), ("interop_disabled", no_interop), ("no_network_listeners", no_listeners),
        ("ssh_disabled", ssh_off), ("runner_user", user), ("docker_engine", docker), ("nvidia_toolkit", toolkit),
        ("runner_installed", runner), ("gpu_passthrough_present", gpu_paths),
    ):
        add(name, fn)
        if name == "distro_registered" and not rep.checks[-1].ok:
            break  # nothing else can be asked of a missing distro
    return rep


# --- GPU smoke test (a tiny CUDA container with nvidia-smi) -------------------------------------------------------- #
def smoke_argv(image: str, gpus_args: Optional[list[str]] = None) -> list[str]:
    """Hardened like a job (ADR-0003): no network, read-only, no capabilities, non-root, bounded. ``--pull never``: the
    image was pulled by digest in an earlier step, so a smoke test never reaches the network."""
    if "@sha256:" not in image:
        raise ValueError("smoke test image must be pinned by @sha256: digest")
    return [
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534", "--pids-limit", "64", "--memory", "512m",
        "--cpus", "1", *(gpus_args or ["--gpus", "all"]), image, "nvidia-smi", "-L",
    ]


def run_gpu_smoke(cli: WslCli, image: str, *, gpus_args: Optional[list[str]] = None, min_gpus: int = 1,
                  runner_user: str = P.RUNNER_USER) -> Check:
    """Run as the RUNNER user (proves it can use Docker). Passes only with exit 0 and at least ``min_gpus`` GPU lines."""
    try:
        res = cli.exec(smoke_argv(image, gpus_args), user=runner_user, timeout=300)
    except Exception as exc:
        return Check("gpu_smoke_test", False, f"could not run: {type(exc).__name__}: {str(exc)[:200]}")
    gpus = parse_gpu_list(res.stdout)
    if res.ok and len(gpus) >= min_gpus:
        return Check("gpu_smoke_test", True, f"{len(gpus)} GPU(s): " + "; ".join(f"{i}:{n}" for i, n, _ in gpus))
    reason = (res.stderr or res.stdout or "no output").strip().replace("\n", " ")[:300]
    return Check("gpu_smoke_test", False, f"exit {res.returncode}, {len(gpus)} GPU line(s): {reason}")


def state_json(text: Optional[str]) -> dict:
    try:
        v = json.loads(text) if text else {}
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}
