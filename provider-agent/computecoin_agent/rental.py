"""Container rental, host side (docs/architecture/RENTAL-container-rental-design.md section 8).

A customer's own container (or a ready template) runs in an ordinary hardened container on a host that opted in. This module holds the
pieces that touch the host's disk and must therefore be careful: names and paths from the network, the workspace, the disk quota, the
safe copy of files out of a directory the customer controls, the export of results, and the miner check.

Trust rules (the customer's code controls everything under the workspace, including creating symlinks and swapping files):

* every path that comes from the network is re-validated here; ``..``, absolute paths, odd characters and link tricks are refused;
* the agent never READS a file in the workspace by a path it could be tricked into following: files leave the workspace only through
  ``copy_regular_nofollow`` (every component opened with ``O_NOFOLLOW`` relative to the previous directory, the result checked to be a regular
  file), into a private staging folder, and the upload reads that private copy;
* the agent writes into the workspace only BEFORE the container starts (staged inputs), through ``resolve_inside``;
* nothing here sends a process name or a file name anywhere except the customer's own storage routes.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .platform import get_platform

logger = logging.getLogger(__name__)

GIB = 1024**3
MIB = 1024**2
MAX_PATH_CHARS = 240
MAX_PART_CHARS = 100
MAX_INPUTS = 64  # the API allows far fewer; this is the agent's own hard stop
MAX_EXPORT_FILES = 2000
MAX_EXPORT_FILE_BYTES = 20 * GIB
MAX_WALK_ENTRIES = 2_000_000  # a workspace with more entries than this is abuse of the disk's inodes, not a dataset
FREE_DISK_MARGIN = GIB
LOW_DISK_BYTES = 512 * MIB  # a rental is stopped when the host's free disk falls below this, whatever the quota says
MAX_SCAN_SKIPS = 200_000  # a results folder full of links and odd names is abuse, not output
RENTAL_PIDS_LIMIT = 2048  # data loaders start many processes; still bounded
DEFAULT_MEMORY_MB = 16384
DEFAULT_CPUS = 4
SHM_CEILING_MB = 2048

_PART_RE = re.compile(r"^[A-Za-z0-9._+=@ -]{1,100}$")
_SID_RE = re.compile(r"^[0-9A-Fa-f-]{8,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

#: Process names (``docker top ... comm``, lower case) that mean cryptocurrency mining. A match stops the rental. Names stay on this host.
MINER_NAMES: tuple[str, ...] = (
    "xmrig", "ethminer", "t-rex", "phoenixminer", "nbminer", "lolminer", "gminer", "cpuminer", "minerd", "cgminer",
    "bfgminer", "kawpowminer", "teamredminer", "srbminer",
)

# Docker flags a rental container must never carry (design note 8.3 step 3). Matched exactly or as ``flag=value``.
_FORBIDDEN_FLAGS = frozenset({
    "--privileged", "-p", "--publish", "-P", "--publish-all", "--device", "--device-cgroup-rule", "--cap-add", "--pid",
    "--userns", "--uts", "--cgroupns", "--volumes-from", "--add-host", "--link", "--sysctl", "--security-opt-unconfined",
})
_FORBIDDEN_VALUES = {  # flag -> values that are refused
    "--network": {"host", "container"}, "--net": {"host", "container"}, "--ipc": {"host"}, "--pid": None, "--userns": None,
}
_DOCKER_SOCKET_HINTS = ("docker.sock", "containerd.sock", "podman.sock")


class RentalError(RuntimeError):
    """A rental could not be set up or a rule was broken. The message is safe to show the host (no customer data)."""


# --------------------------------------------------------------------------- names and paths
def clean_relpath(value: Any, *, allow_results_prefix: bool = True) -> str:
    """A clean relative POSIX path from the network, or ``RentalError``. Same rules as the API: no leading ``/``, no ``..`` or ``.``, no empty
    part, no backslash or control character, each part at most 100 characters of ``[A-Za-z0-9._+=@ -]`` and not starting or ending with a
    space, at most 240 characters in all. ``allow_results_prefix=False`` also refuses a first part of ``results`` (inputs may not land there)."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise RentalError("a file path is empty or too long")
    if value.startswith("/") or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RentalError("a file path must be relative and plain")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", "..") or part != part.strip() or len(part) > MAX_PART_CHARS or not _PART_RE.match(part):
            raise RentalError("a file path has a part that is not allowed")
    if not allow_results_prefix and parts[0] == "results":
        raise RentalError("a file may not be placed in the results folder")
    return "/".join(parts)


def resolve_inside(root: str, rel: str) -> str:
    """``root/rel`` after checking that no existing component on the way is a symlink and that the result stays inside ``root``."""
    rel = clean_relpath(rel)
    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        try:
            mode = os.lstat(cur).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise RentalError("a path in the workspace goes through a link")
    real_root = os.path.realpath(root)
    if not os.path.realpath(cur).startswith(real_root + os.sep):
        raise RentalError("a path escapes the workspace")
    return cur


# Command-line markers that only a miner talking to a pool has. A rental has no network except package sites, so this is a second net.
MINER_ARG_MARKERS: tuple[str, ...] = ("stratum+tcp://", "stratum+ssl://", "stratum2+tcp://", "stratum+tls://", "stratum1+tcp://")


def detect_miner(top_output: str) -> Optional[str]:
    """The matching miner pattern found in ``docker top ... -eo comm,args`` output (or ``comm`` alone), else None. A line is
    ``<process name> <its arguments>``. Only the pattern is returned, never the process name or arguments."""
    for line in (top_output or "").splitlines():
        text = line.strip().lower()
        if not text:
            continue
        name = os.path.basename(text.split(None, 1)[0])
        if name in ("comm", "command"):
            continue
        for miner in MINER_NAMES:
            if name == miner or name.startswith(miner):
                return miner
        for marker in MINER_ARG_MARKERS:
            if marker in text:
                return marker
    return None


# --------------------------------------------------------------------------- argv safety
def assert_rental_argv_safe(argv: list[str], allowed_bind_sources: list[str]) -> None:
    """Refuse a rental container command that carries any dangerous flag, any bind mount other than the two this agent made, or a path
    that looks like a container-engine socket."""
    allowed = {os.path.realpath(p) for p in allowed_bind_sources}
    i = 0
    while i < len(argv):
        arg = argv[i]
        flag, eq, value = arg.partition("=")
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if flag in _FORBIDDEN_FLAGS or (flag == "--security-opt" and "unconfined" in (value if eq else nxt)):
            raise RentalError(f"a rental container may not use {flag}")
        if flag in _FORBIDDEN_VALUES:
            bad = _FORBIDDEN_VALUES[flag]
            val = value if eq else nxt
            if bad is None or val in bad or val.startswith("container:"):
                raise RentalError(f"a rental container may not use {flag} {val}")
        if any(h in arg for h in _DOCKER_SOCKET_HINTS):
            raise RentalError("a rental container may not reach the container engine")
        if flag in ("--mount", "-v", "--volume"):
            spec = value if eq else nxt
            if flag == "--mount":
                fields = dict(kv.partition("=")[::2] for kv in spec.split(","))
                if fields.get("type") != "bind":
                    raise RentalError("a rental container may only use the two folders the agent made")
                source = fields.get("source") or fields.get("src") or ""
            else:
                source = spec.split(":", 1)[0]
            if os.path.realpath(source) not in allowed:
                raise RentalError("a rental container may only use the two folders the agent made")
        i += 1


# --------------------------------------------------------------------------- limits
@dataclass(frozen=True)
class RentalSettings:
    """What this host allows, from the agent settings."""

    allowed: bool
    root: str
    max_workspace_gb: int = 50
    max_memory_gb: int = 0
    max_cpus: int = 0

    @classmethod
    def from_config(cls, config: Any) -> "RentalSettings":
        root = getattr(config, "rental_root", None)
        if not root:
            from pathlib import Path

            base = getattr(config, "work_dir", None) or str(get_platform().paths.data_dir())
            root = str(Path(base) / "rental")
        return cls(
            allowed=getattr(config, "allow_container_rental", False) is True,
            root=str(root),
            max_workspace_gb=int(getattr(config, "rental_max_workspace_gb", 50) or 50),
            max_memory_gb=int(getattr(config, "rental_max_memory_gb", 0) or 0),
            max_cpus=int(getattr(config, "rental_max_cpus", 0) or 0),
        )


@dataclass
class RentalRun:
    """The resolved limits and workspace of one running rental."""

    workspace: "Workspace"
    memory_mb: int
    cpus: int
    shm_mb: int
    quota_bytes: int
    lock: threading.Lock = field(default_factory=threading.Lock)  # one export at a time
    saved: dict = field(default_factory=dict)  # results name -> (size, mtime_ns) of the copy last saved; unchanged files are not sent again


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def resolve_limits(settings: RentalSettings, claim: dict[str, Any], *, total_memory_mb: Optional[int] = None,
                   cpu_count: Optional[int] = None) -> tuple[int, int, int, int]:
    """``(memory_mb, cpus, shm_mb, quota_bytes)``: what the customer asked for, capped by what this host allows."""
    if total_memory_mb is None:
        with contextlib.suppress(Exception):
            total_memory_mb = get_platform().hardware.memory_total_mb()
    cpu_count = cpu_count or os.cpu_count() or 1
    mem_cap = settings.max_memory_gb * 1024 if settings.max_memory_gb > 0 else (int(total_memory_mb * 0.9) if total_memory_mb else 262144)
    cpu_cap = settings.max_cpus if settings.max_cpus > 0 else max(1, cpu_count - 1)
    memory_mb = max(256, min(max(_int(claim.get("memory_mb"), DEFAULT_MEMORY_MB), 256), mem_cap))
    cpus = max(1, min(max(_int(claim.get("cpus"), DEFAULT_CPUS), 1), cpu_cap))
    shm_mb = max(64, min(SHM_CEILING_MB, memory_mb // 4))
    granted_gb = _int(claim.get("workspace_gb"), 0)
    quota_gb = min(granted_gb if granted_gb > 0 else 1, max(1, settings.max_workspace_gb))  # a rental always has a workspace; 0 means the 1 GiB floor
    return memory_mb, cpus, shm_mb, quota_gb * GIB


# --------------------------------------------------------------------------- the workspace
def _mkdir_open(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    get_platform().paths.set_mode(path, 0o777)  # the container user is not this user


def walk_size(path: str, *, max_entries: int = MAX_WALK_ENTRIES) -> tuple[int, int, bool]:
    """``(bytes_on_disk, entries, too_many)`` under ``path``, without following links. Uses allocated blocks where the OS reports them, so a
    sparse file cannot hide its size and a huge sparse file is not over-counted."""
    total = entries = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    entries += 1
                    if entries > max_entries:
                        return total, entries, True
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += getattr(st, "st_blocks", 0) * 512 if hasattr(st, "st_blocks") else st.st_size
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
        except OSError:
            continue
    return total, entries, False


class Workspace:
    """``<root>/<session id>/workspace`` (what the container sees) next to ``<root>/<session id>/stage`` (private to the agent)."""

    def __init__(self, base: str, sid: str, quota_bytes: int) -> None:
        if not _SID_RE.match(str(sid)):
            raise RentalError("the session id is not usable as a folder name")
        if "," in base or "\n" in base or "\x00" in base:
            raise RentalError("the rental folder path may not contain a comma or a line break")
        self.sid = str(sid)
        self.base = base
        self.top = os.path.join(base, self.sid)
        self.path = os.path.join(self.top, "workspace")
        self.stage = os.path.join(self.top, "stage")
        self.quota_bytes = int(quota_bytes)

    def create(self, *, need_free_bytes: int = 0) -> None:
        os.makedirs(self.base, mode=0o700, exist_ok=True)
        free = shutil.disk_usage(self.base).free
        if free < need_free_bytes + FREE_DISK_MARGIN:
            raise RentalError("this computer does not have enough free disk space for that rental")
        os.makedirs(self.top, mode=0o700, exist_ok=False)  # a leftover of the same id is an error, never reused
        os.makedirs(self.stage, mode=0o700)
        _mkdir_open(self.path)
        for sub in (".tmp", "results"):
            _mkdir_open(os.path.join(self.path, sub))

    def size(self) -> tuple[int, int, bool]:
        return walk_size(self.path)

    def over_quota(self) -> bool:
        used, _entries, too_many = self.size()
        return too_many or used > self.quota_bytes

    def disk_low(self) -> bool:
        """The host's own disk is nearly full (a fast writer can outrun the periodic quota check)."""
        try:
            return shutil.disk_usage(self.base).free < LOW_DISK_BYTES
        except OSError:
            return False

    def delete(self) -> None:
        shutil.rmtree(self.top, ignore_errors=True)


def list_stale(base: str, live_names: set[str]) -> list[str]:
    """Rental folders under ``base`` whose container no longer exists (an agent that died leaves them behind)."""
    out: list[str] = []
    try:
        for entry in os.scandir(base):
            if entry.is_dir(follow_symlinks=False) and _SID_RE.match(entry.name) and f"ccoin-sess-{entry.name}" not in live_names:
                out.append(entry.path)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- safe copy out of the workspace
def copy_regular_nofollow(root: str, rel: str, dest: str, *, max_bytes: int = MAX_EXPORT_FILE_BYTES, chunk: int = MIB) -> int:
    """Copy ``root/rel`` to the private file ``dest`` and return its size. Every path component is opened relative to the previous
    directory with ``O_NOFOLLOW``, and the final object must be a regular file, so a customer who swaps a file or folder for a link
    cannot make the agent read anything outside the workspace. A file that grows past ``max_bytes`` while it is copied is refused."""
    rel = clean_relpath(rel)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RentalError("this system cannot open files without following links")
    base_flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    dirflags = base_flags | getattr(os, "O_DIRECTORY", 0)
    parts = rel.split("/")
    fds: list[int] = []
    try:
        cur = os.open(root, dirflags)
        fds.append(cur)
        for part in parts[:-1]:
            cur = os.open(part, dirflags, dir_fd=cur)
            fds.append(cur)
        ffd = os.open(parts[-1], base_flags | getattr(os, "O_NONBLOCK", 0), dir_fd=cur)  # O_NONBLOCK: a FIFO must not hang the agent
        fds.append(ffd)
        st = os.fstat(ffd)
        if not stat.S_ISREG(st.st_mode):
            raise RentalError("not a regular file")
        if st.st_size > max_bytes:
            raise RentalError("a file is too large to save")
        total = 0
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(out, "wb") as fh:
                while True:
                    data = os.read(ffd, chunk)
                    if not data:
                        break
                    total += len(data)
                    if total > max_bytes:
                        raise RentalError("a file grew too large while it was being saved")
                    fh.write(data)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(dest)
            raise
        return total
    except OSError as exc:  # a link, a vanished file, a permission error: all "cannot save this one"
        raise RentalError(f"a file could not be read safely ({type(exc).__name__})") from exc
    finally:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                os.close(fd)


def list_results(results_dir: str, *, max_files: int = MAX_EXPORT_FILES) -> tuple[list[str], int, bool]:
    """``(names, skipped, truncated)``: regular files under ``results_dir`` as clean paths relative to it. Links, special files and names
    that fail the path rules are skipped (and counted); the walk never follows a link. At most ``max_files`` are returned."""
    names: list[str] = []
    skipped = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(results_dir, topdown=True, followlinks=False):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            try:
                if stat.S_ISLNK(os.lstat(full).st_mode):
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            keep.append(d)
        if skipped > MAX_SCAN_SKIPS:
            return names, skipped, True
        dirnames[:] = sorted(keep)
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            try:
                regular = stat.S_ISREG(os.lstat(full).st_mode)
                rel = os.path.relpath(full, results_dir).replace(os.sep, "/")
                clean_relpath(rel)
            except (OSError, RentalError):
                skipped += 1
                if skipped > MAX_SCAN_SKIPS:
                    return names, skipped, True
                continue
            if not regular:
                skipped += 1
                continue
            if len(names) >= max_files:
                truncated = True
                return names, skipped, truncated
            names.append(rel)
    return names, skipped, truncated


# --------------------------------------------------------------------------- upload adapter and export
class SessionFileApi:
    """Makes ``runtime_v2.upload_file`` (written for ``/jobs/<id>/uploads...``) talk to ``/v1/agent/sessions/<id>/uploads...``, signed by the
    same machine key as every other session call."""

    def __init__(self, api: Any, sid: str) -> None:
        self.api, self.sid = api, str(sid)

    def _map(self, path: str) -> str:
        prefix = f"/jobs/{self.sid}/uploads"
        if not path.startswith(prefix):
            raise RentalError("unexpected upload path")
        return f"/{self.sid}/uploads" + path[len(prefix):]

    def post(self, path: str, body: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
        return self.api.post(self._map(path), body)

    def ok(self, path: str, body: Optional[dict[str, Any]] = None, *, code: str) -> dict[str, Any]:
        from .runtime_v2 import V2Error

        status, data = self.post(path, body)
        if status != 200:
            raise V2Error(code, f"upload: HTTP {status} {data.get('detail', '')}")
        return data


@dataclass
class ExportOutcome:
    files: list[dict[str, Any]] = field(default_factory=list)
    failed: bool = False
    error: Optional[str] = None


def export_results(
    api: Any,
    sid: str,
    run: RentalRun,
    *,
    upload: Optional[Callable[..., dict[str, Any]]] = None,
    transport: Any = None,
    sleeper: Callable[[float], None] = time.sleep,
    max_files: int = MAX_EXPORT_FILES,
    max_file_bytes: int = MAX_EXPORT_FILE_BYTES,
    quiet_if_unchanged: bool = False,
) -> ExportOutcome:
    """Save every regular file under the workspace's ``results`` folder to the customer's storage and report it. Never raises: a failure is
    returned as ``failed`` so ending the session is never blocked. One export at a time per rental. Files whose size and modified time
    are the same as when they were last saved are not sent again (the control plane keeps what it already has). ``quiet_if_unchanged``
    (the automatic save) then makes no call at all when nothing changed."""
    from .runtime_v2 import V2Error, upload_file

    upload = upload or upload_file
    outcome = ExportOutcome()
    with run.lock:
        ws = run.workspace
        try:
            names, skipped, truncated = list_results(os.path.join(ws.path, "results"), max_files=max_files)
        except OSError:
            names, skipped, truncated = [], 0, False
        results_dir = os.path.join(ws.path, "results")

        def _stamp(name: str) -> Optional[tuple[int, int]]:
            try:
                st = os.lstat(os.path.join(results_dir, name))
            except OSError:
                return None
            return (st.st_size, st.st_mtime_ns)

        stamps = {n: _stamp(n) for n in names}
        changed = [n for n in names if stamps[n] is None or run.saved.get(n) != stamps[n]]
        if quiet_if_unchanged and not changed and not truncated:
            return outcome
        with contextlib.suppress(Exception):
            api.post(f"/{sid}/export-start")
        names = changed
        errors = 0
        adapter = SessionFileApi(api, sid)
        for i, name in enumerate(names):
            staged = os.path.join(ws.stage, f"export-{i}")
            try:
                size = copy_regular_nofollow(ws.path + os.sep + "results", name, staged, max_bytes=max_file_bytes)
                if size <= 0:
                    if stamps[name] is not None:
                        run.saved[name] = stamps[name]  # nothing to save, and nothing to retry until it changes
                    continue  # the store does not take empty files
                info = upload(adapter, sid, staged, purpose="output", name=name, transport=transport, sleeper=sleeper)
                outcome.files.append({"name": name, "size_bytes": int(info.get("size", size)), "sha256": info.get("sha256", ""),
                                      "file_id": str(info.get("file_id", ""))})
                if stamps[name] is not None:
                    run.saved[name] = stamps[name]  # the stamp taken BEFORE the copy: a file changed since is sent again next time
            except (RentalError, V2Error, OSError) as exc:
                errors += 1
                logger.warning("export: a file could not be saved (%s)", type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - never let one file end the export
                errors += 1
                logger.warning("export: a file could not be saved (%s)", type(exc).__name__)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(staged)
        if errors or truncated:
            outcome.failed = True
            outcome.error = ("some files could not be saved" if errors else "too many files; only the first were saved")[:200]
        try:
            api.post(f"/{sid}/export-result", {"files": outcome.files, "failed": outcome.failed, "error": outcome.error})
        except Exception as exc:  # noqa: BLE001
            logger.warning("export: could not report the result (%s)", type(exc).__name__)
            outcome.failed = True
            outcome.error = outcome.error or "the result could not be reported"
    return outcome
