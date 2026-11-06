"""Helpers for the Windows host end-to-end tests (cross-platform plan W15). WRITTEN, NOT RUN.

Nothing here runs at import time. Every external command is an argument list (never a shell string), every setting comes from the
environment, and secrets are only ever read from environment variables by name and never echoed (L7).

TODO(verify-docs) (X11): the ``wsl.exe`` options used below (``-d <distro>``, ``--exec``, ``--list --verbose``) against Microsoft's
current command reference (https://learn.microsoft.com/windows/wsl/basic-commands), and the ``nvidia-smi`` output format.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional, Sequence


# --------------------------------------------------------------------------- settings from the environment
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    wsl_exe: str = "wsl.exe"
    distro: str = "computecoin"
    agent_cmd: tuple[str, ...] = ("computecoin-agent",)
    probe_image: Optional[str] = None  # must be pinned by digest: checked by pinned_image()
    kit_image: Optional[str] = None  # a real Kit image@digest for the one-job test
    kit_job_cmd: Optional[str] = None  # command run INSIDE the distro: runs one Kit job, prints one JSON line (see test_windows_host_provisioning)
    escape_suite_cmd: Optional[str] = None  # command run INSIDE the distro, e.g. "cd /opt/computecoin && pytest -q provider-agent/tests/escape"
    artifact_dir: Path = Path("e2e-artifacts")
    linux_artifact_dir: Optional[Path] = None

    @classmethod
    def from_env(cls) -> "Settings":
        agent = _env("CC_AGENT_CMD")
        linux = _env("CC_LINUX_ARTIFACT_DIR")
        return cls(
            wsl_exe=_env("CC_WSL_EXE", "wsl.exe") or "wsl.exe",
            distro=_env("CC_DISTRO", "computecoin") or "computecoin",
            agent_cmd=tuple(agent.split()) if agent else ("computecoin-agent",),
            probe_image=_env("CC_PROBE_IMAGE"),
            kit_image=_env("CC_KIT_IMAGE"),
            kit_job_cmd=_env("CC_KIT_JOB_CMD"),
            escape_suite_cmd=_env("CC_ESCAPE_SUITE_CMD"),
            artifact_dir=Path(_env("CC_ARTIFACT_DIR", "e2e-artifacts") or "e2e-artifacts"),
            linux_artifact_dir=Path(linux) if linux else None,
        )


_DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}$")


def pinned_image(ref: Optional[str], what: str) -> str:
    """An image reference, refused unless it is pinned by digest (X10, L4): a moving tag would make a test result meaningless."""
    if not ref or not _DIGEST_PIN.search(ref):
        raise ValueError(f"{what} must be set to an image pinned by @sha256:<64 hex>")
    return ref


def require_secret(name: str) -> str:
    """A secret from the environment. The error never contains the value; an empty or missing one fails the step."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


def scrub(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[REDACTED]")
    return text


# --------------------------------------------------------------------------- running things
@dataclass
class Run:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


def decode_console(raw: bytes) -> str:
    """wsl.exe management commands may print UTF-16 LE; everything else is UTF-8."""
    if b"\x00" in raw:
        for enc in ("utf-16-le", "utf-16"):
            try:
                return raw.decode(enc).lstrip("﻿").replace("\r\n", "\n")
            except UnicodeDecodeError:
                continue
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


def run(argv: Sequence[str], *, timeout: float = 600.0, input_bytes: Optional[bytes] = None, env: Optional[Mapping[str, str]] = None) -> Run:
    """Run a command without a shell. A timeout or a missing program is a failed Run, not an exception (the test decides)."""
    full_env = dict(os.environ)
    full_env["WSL_UTF8"] = "1"  # TODO(verify-docs): honoured by current WSL releases
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(list(argv), input=input_bytes, capture_output=True, timeout=timeout, shell=False, env=full_env, check=False)
    except subprocess.TimeoutExpired:
        return Run(list(argv), 124, "", f"timed out after {timeout}s")
    except OSError as exc:
        return Run(list(argv), 127, "", f"could not start {argv[0]!r}: {exc}")
    return Run(list(argv), proc.returncode, decode_console(proc.stdout), decode_console(proc.stderr))


class Wsl:
    """Run commands in the managed distro. Guest paths are PurePosixPath strings (X9)."""

    def __init__(self, s: Settings) -> None:
        self.s = s

    def host(self, *args: str, timeout: float = 120.0) -> Run:
        return run([self.s.wsl_exe, *args], timeout=timeout)

    def exec(self, *argv: str, timeout: float = 600.0, user: Optional[str] = None) -> Run:
        cmd = [self.s.wsl_exe, "-d", self.s.distro]
        if user:
            cmd += ["-u", user]
        return run([*cmd, "--exec", *argv], timeout=timeout)

    def sh(self, script: str, *, timeout: float = 600.0, user: Optional[str] = None) -> Run:
        return self.exec("/bin/sh", "-c", script, timeout=timeout, user=user)

    def read_file(self, path: str) -> str:
        assert PurePosixPath(path).is_absolute(), "guest paths are absolute POSIX paths"
        return self.exec("/bin/cat", path).stdout

    def distro_listed(self) -> bool:
        res = self.host("--list", "--verbose")
        return res.ok and any(self.s.distro in line.split() for line in res.stdout.splitlines())


# --------------------------------------------------------------------------- hashing and parity
def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:  # binary: no newline translation, ever
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def hash_tree(root: Path, *, ignore: Iterable[str] = ()) -> dict[str, str]:
    """``{posix relative path: sha256}`` of every file under ``root`` (keys use "/" on every OS so manifests compare across hosts)."""
    skip = set(ignore)
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(root).as_posix()
            if rel not in skip:
                out[rel] = sha256_file(p)
    return out


@dataclass
class ParityReport:
    equal: list[str] = field(default_factory=list)
    different: list[str] = field(default_factory=list)
    only_windows: list[str] = field(default_factory=list)
    only_linux: list[str] = field(default_factory=list)
    skipped_nondeterministic: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.equal) and not (self.different or self.only_windows or self.only_linux)

    def summary(self) -> str:
        return (f"equal={len(self.equal)} different={self.different} only_windows={self.only_windows} "
                f"only_linux={self.only_linux} skipped={self.skipped_nondeterministic}")


def compare_manifests(windows: Mapping[str, str], linux: Mapping[str, str], *, nondeterministic: Iterable[str] = ()) -> ParityReport:
    """Parity of two ``hash_tree`` manifests. Files named in ``nondeterministic`` (a Kit's declared timestamps or logs) are listed but
    not compared; nothing else is excused. An empty comparison is a FAIL: no files means nothing was proven (L1)."""
    skip = set(nondeterministic)
    rep = ParityReport()
    for name in sorted(set(windows) | set(linux)):
        if name in skip:
            rep.skipped_nondeterministic.append(name)
        elif name not in linux:
            rep.only_windows.append(name)
        elif name not in windows:
            rep.only_linux.append(name)
        elif windows[name] == linux[name]:
            rep.equal.append(name)
        else:
            rep.different.append(name)
    return rep


def write_manifest(path: Path, manifest: Mapping[str, str]) -> None:
    path.write_bytes((json.dumps(dict(sorted(manifest.items())), indent=2) + "\n").encode("utf-8"))  # bytes: LF on every OS


def read_manifest(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


# --------------------------------------------------------------------------- isolation probes (run INSIDE a job-like container)
# The probe prints one JSON object. Each value is True when the thing was REACHABLE, which is the failure.
# It is plain stdlib Python so any pinned python image can run it: docker run --rm --network none --read-only ... python -c <probe>
ISOLATION_PROBE_PY = r'''
import json, os, socket, subprocess, sys

def exists(p):
    try:
        return os.path.exists(p)
    except OSError:
        return False

def listdir_nonempty(p):
    try:
        return len(os.listdir(p)) > 0
    except OSError:
        return False

def connects(host, port):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()

def runs(argv):
    try:
        subprocess.run(argv, capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False

gateway = os.environ.get("PROBE_LAN_HOST", "")
out = {
    "windows_drive_mount_c": exists("/mnt/c/Windows"),
    "windows_mnt_not_empty": listdir_nonempty("/mnt"),
    "windows_interop_cmd": runs(["/mnt/c/Windows/System32/cmd.exe", "/c", "echo", "x"]),
    "windows_path_in_PATH": any(p.startswith("/mnt/") for p in os.environ.get("PATH", "").split(":")),
    "dxg_device_visible": exists("/dev/dxg"),  # informational: the GPU path, not a Windows file
    "lan_host_reachable": bool(gateway) and connects(gateway, int(os.environ.get("PROBE_LAN_PORT", "445"))),
    "internet_reachable": connects("1.1.1.1", 443),
    "docker_socket_visible": exists("/var/run/docker.sock"),
    "wsl_interop_socket": exists("/run/WSL"),
}
print(json.dumps(out, sort_keys=True))
'''

# TODO(verify-docs): the interop paths probed above (/mnt/c, /run/WSL, /mnt/c/Windows/System32/cmd.exe) are assumptions about a
# default WSL2 distro; confirm on the real runtime before trusting a pass.
# Keys whose True means the job reached something it must not (X6, ADR-0003). ``dxg_device_visible`` is informational only.
MUST_BE_FALSE = (
    "windows_drive_mount_c", "windows_mnt_not_empty", "windows_interop_cmd", "windows_path_in_PATH",
    "lan_host_reachable", "internet_reachable", "docker_socket_visible", "wsl_interop_socket",
)


def probe_violations(result: Mapping[str, object]) -> list[str]:
    """Names of probe results that are not exactly False. A missing key counts as a violation (fail closed)."""
    return [k for k in MUST_BE_FALSE if result.get(k) is not False]


# --------------------------------------------------------------------------- soak bookkeeping
def now() -> float:
    return time.time()
