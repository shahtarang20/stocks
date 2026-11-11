"""``wsl.exe`` access: argv builders, the injectable runner and output decoding.

Rules (X11, never shell strings):

* every command is an argv ``list[str]`` handed to ``subprocess.run(..., shell=False)``;
* commands inside the distro use ``wsl.exe --distribution <name> --user <user> --exec <program> <args...>``: ``--exec``
  runs the program directly, with no shell between wsl.exe and the program;
* a shell program (``sh``, ``bash``, ``cmd``, ``powershell`` ...) as the in-distro program is refused by
  :func:`assert_no_shell`, so a future edit cannot smuggle one in;
* file content is written through stdin (``tee``), not through ``echo`` or redirection.

TODO(verify-docs) (X11) for every flag below, against Microsoft's current ``wsl.exe`` command reference
(https://learn.microsoft.com/windows/wsl/basic-commands): ``--import <Distro> <InstallLocation> <TarFile> --version 2``,
``--unregister``, ``--terminate``, ``--list --verbose``, ``--version``, ``--manage <Distro> --resize`` (needs a recent
WSL release), ``--manage <Distro> --set-sparse``, ``--distribution``, ``--user``, ``--exec``, and the ``WSL_UTF8``
environment variable that switches management output from UTF-16LE to UTF-8. None of it was checked or run.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence

DEFAULT_WSL_EXE = "wsl.exe"
SHELL_PROGRAMS = frozenset({
    "sh", "bash", "dash", "zsh", "ash", "ksh", "fish", "csh", "tcsh", "busybox",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
})


class WslError(RuntimeError):
    """A wsl.exe command failed or was refused."""


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    """The injectable exec layer. ``argv`` is always a list of str; implementations must never use a shell."""

    def __call__(self, argv: Sequence[str], *, stdin: Optional[bytes] = None, timeout: Optional[float] = None) -> RunResult:
        ...


def decode_wsl_output(raw: bytes) -> str:
    """wsl.exe management commands may print UTF-16LE (with NUL bytes) unless ``WSL_UTF8=1`` is honoured."""
    if not raw:
        return ""
    if b"\x00" in raw:
        for enc in ("utf-16-le", "utf-16"):
            try:
                return raw.decode(enc).lstrip("﻿").replace("\r\n", "\n")
            except UnicodeDecodeError:
                continue
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


def subprocess_runner(argv: Sequence[str], *, stdin: Optional[bytes] = None, timeout: Optional[float] = None) -> RunResult:  # pragma: no cover - runs wsl.exe
    import os

    env = {**os.environ, "WSL_UTF8": "1"}  # TODO(verify-docs): honoured by current WSL releases
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False
            list(argv), input=stdin, capture_output=True, timeout=timeout, shell=False, env=env, creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timed out after {timeout}s")
    except OSError as exc:
        return RunResult(127, "", f"could not start {argv[0]!r}: {exc}")
    return RunResult(proc.returncode, decode_wsl_output(proc.stdout), decode_wsl_output(proc.stderr))


def assert_no_shell(program_and_args: Sequence[str]) -> None:
    """Refuse a shell as the in-distro program, and an empty or non-str argv."""
    if not program_and_args or not all(isinstance(a, str) for a in program_and_args):
        raise WslError("argv must be a non-empty list of str")
    name = program_and_args[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in SHELL_PROGRAMS:
        raise WslError(f"refusing to run a shell ({name}) inside the runtime distro")


# --- pure argv builders (the exact wsl.exe command lines) ------------------------------------------------------ #
def argv_import(distro: str, install_dir: str, tarball: str, wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    return [wsl_exe, "--import", distro, install_dir, tarball, "--version", "2"]


def argv_unregister(distro: str, wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    return [wsl_exe, "--unregister", distro]


def argv_terminate(distro: str, wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    """Stops ONLY this distro. We never call ``wsl --shutdown``: it would stop the owner's other distros."""
    return [wsl_exe, "--terminate", distro]


def argv_list(wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    return [wsl_exe, "--list", "--verbose"]


def argv_version(wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    return [wsl_exe, "--version"]


def argv_status(wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    return [wsl_exe, "--status"]


def argv_resize(distro: str, size: str, wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    return [wsl_exe, "--manage", distro, "--resize", size]


def argv_set_sparse(distro: str, enabled: bool, wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    return [wsl_exe, "--manage", distro, "--set-sparse", "true" if enabled else "false"]


def argv_exec(distro: str, program_and_args: Sequence[str], user: str = "root", wsl_exe: str = DEFAULT_WSL_EXE) -> list[str]:
    assert_no_shell(program_and_args)
    return [wsl_exe, "--distribution", distro, "--user", user, "--exec", *program_and_args]


# --- parsing --------------------------------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DistroInfo:
    name: str
    state: str
    version: int
    default: bool


def parse_list_verbose(text: str) -> list[DistroInfo]:
    """Parse ``wsl --list --verbose``. Header line and blank lines are skipped. TODO(verify-docs): output is localised."""
    out: list[DistroInfo] = []
    for line in text.replace("\x00", "").splitlines():
        if not line.strip():
            continue
        default = line.lstrip().startswith("*")
        parts = line.replace("*", " ", 1).split()
        if len(parts) < 3 or not parts[-1].isdigit():
            continue  # header or a localised message line
        out.append(DistroInfo(name=parts[0], state=parts[1], version=int(parts[-1]), default=default))
    return out


_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version_tuple(text: str) -> Optional[tuple[int, ...]]:
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def version_at_least(have: Optional[tuple[int, ...]], want: str) -> bool:
    need = parse_version_tuple(want)
    if have is None or need is None:
        return False
    width = max(len(have), len(need))
    return tuple(have) + (0,) * (width - len(have)) >= tuple(need) + (0,) * (width - len(need))


class WslCli:
    """Thin, injectable wrapper: every method builds an argv list and hands it to the runner."""

    def __init__(self, runner: Runner, distro: str, *, wsl_exe: str = DEFAULT_WSL_EXE, default_timeout: float = 600.0) -> None:
        self.run = runner
        self.distro = distro
        self.wsl_exe = wsl_exe
        self.default_timeout = default_timeout

    # host-side wsl.exe commands
    def host(self, argv: Sequence[str], timeout: Optional[float] = None) -> RunResult:
        return self.run(list(argv), stdin=None, timeout=timeout or self.default_timeout)

    def version(self) -> Optional[tuple[int, ...]]:
        res = self.host(argv_version(self.wsl_exe), timeout=30)
        return parse_version_tuple(res.stdout) if res.ok else None

    def distros(self) -> list[DistroInfo]:
        res = self.host(argv_list(self.wsl_exe), timeout=30)
        return parse_list_verbose(res.stdout) if res.ok else []

    def distro_info(self) -> Optional[DistroInfo]:
        return next((d for d in self.distros() if d.name == self.distro), None)

    def terminate(self) -> RunResult:
        return self.host(argv_terminate(self.distro, self.wsl_exe), timeout=60)

    # commands inside the distro, as root unless told otherwise
    def exec(self, program_and_args: Sequence[str], *, user: str = "root", stdin: Optional[bytes] = None,
             timeout: Optional[float] = None) -> RunResult:
        return self.run(argv_exec(self.distro, program_and_args, user, self.wsl_exe), stdin=stdin,
                        timeout=timeout or self.default_timeout)

    def exec_ok(self, program_and_args: Sequence[str], *, what: str, user: str = "root", stdin: Optional[bytes] = None,
                timeout: Optional[float] = None) -> RunResult:
        res = self.exec(program_and_args, user=user, stdin=stdin, timeout=timeout)
        if not res.ok:
            detail = (res.stderr or res.stdout or "").strip().replace("\n", " ")[:300]
            raise WslError(f"{what} failed (exit {res.returncode}): {detail}")
        return res

    def read_file(self, path: str) -> Optional[str]:
        res = self.exec(["cat", path], timeout=30)
        return res.stdout if res.ok else None

    def write_file(self, path: str, data: bytes, *, mode: str = "0644", owner: str = "root:root") -> None:
        """Write ``data`` through stdin: ``mkdir -p`` the directory, ``dd`` into a temp file, ``chmod``, ``chown``, ``mv``.

        ``dd of=`` and not ``tee``: ``tee`` copies everything it reads back to its stdout, and ``exec`` captures stdout, so
        a large payload (the agent wheel, up to 512 MiB) was held in memory a second time and sent back through the pipe.
        ``dd`` prints nothing with ``status=none``; ``bs=1M iflag=fullblock`` keeps it fast on a pipe.
        """
        directory, _, _name = path.rpartition("/")
        tmp = f"{path}.cc-tmp"
        if directory:
            self.exec_ok(["mkdir", "-p", directory], what=f"create {directory}", timeout=30)
        self.exec_ok(
            ["dd", f"of={tmp}", "bs=1M", "iflag=fullblock", "status=none"], what=f"write {path}", stdin=data, timeout=120
        )
        self.exec_ok(["chmod", mode, tmp], what=f"chmod {path}", timeout=30)
        self.exec_ok(["chown", owner, tmp], what=f"chown {path}", timeout=30)
        self.exec_ok(["mv", "-f", tmp, path], what=f"install {path}", timeout=30)
