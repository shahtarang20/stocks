"""Agent runtime v2 (U3): data-plane staging, per-kit host cache, checkpoints and output upload.

Design: docs/architecture/U3-agent-runtime-v2-design.md. Only used when the claim carries a ``v2`` plan; a
v1 job never reaches this module.

* Inputs and Kit assets are downloaded straight from the store with presigned URLs (bytes never pass through
  the API), verified (size and SHA-256) before use and bind-mounted READ-ONLY into the container.
* Kit assets live in a per-kit, content-addressed host cache with a size limit and LRU eviction, so they are
  not downloaded twice. Customer inputs are job-private and deleted afterwards unless the plan marks them
  ``cacheable``.
* While the container runs, the Kit's checkpoint dir is sealed into a tar and uploaded through the data plane;
  on a new attempt (another host) the newest proven checkpoint is restored before the job starts.
* Declared outputs are collected, hashed and uploaded through the data plane.
* Egress uses the existing B5 machinery in executor.py (this module only sets ``network_policy``).

Presigned URLs are bearer secrets: they are never logged and never placed in argv or container env.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .executor import ExecutionResult, JobSpec, check_mount_target, ExecutorError
from .platform import get_platform

PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_KIT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_EGRESS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}:[0-9]{1,5}$")
CHUNK = 1024 * 1024
COMMIT_MARKER = "COMMIT"
QUIET_SECONDS = 2.0
IN_TARGET = "/in"
CACHE_TARGET = "/cache"


class V2Error(RuntimeError):
    """A v2 step failed; ``code`` is a short stable string that becomes the job error code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- #
# Plan validation
# --------------------------------------------------------------------------- #
@dataclass
class Plan:
    kit: str
    inputs: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    egress: list[str]
    checkpoint_dir: Optional[str]
    checkpoint_interval: int
    resume: Optional[dict[str, Any]]


def _check_item(item: Any, *, asset: bool) -> dict[str, Any]:
    if not isinstance(item, dict) or not _NAME.match(str(item.get("name", ""))):
        raise V2Error("invalid_v2_plan", "bad item name")
    if not _SHA.match(str(item.get("sha256", ""))) or not isinstance(item.get("size_bytes"), int) or item["size_bytes"] < 0:
        raise V2Error("invalid_v2_plan", "bad item hash or size")
    return {**item, "cacheable": True if asset else bool(item.get("cacheable", False))}


def parse_plan(v2: dict[str, Any], *, min_interval: int) -> Plan:
    kit = v2.get("kit")
    slug = kit.get("slug") if isinstance(kit, dict) else None
    if not isinstance(slug, str) or not _KIT.match(slug):
        raise V2Error("invalid_v2_plan", "bad kit")
    inputs = [_check_item(i, asset=False) for i in v2.get("inputs") or []]
    assets = [_check_item(i, asset=True) for i in v2.get("assets") or []]
    names = [i["name"] for i in inputs]
    if len(set(names)) != len(names) or len({a["name"] for a in assets}) != len(assets):
        raise V2Error("invalid_v2_plan", "duplicate names")
    outputs = []
    for o in v2.get("outputs") or []:
        if not isinstance(o, dict) or not _NAME.match(str(o.get("name", ""))):
            raise V2Error("invalid_v2_plan", "bad output")
        path = str(o.get("path", ""))
        if not path or path.startswith("/") or ".." in path.split("/") or "\x00" in path:
            raise V2Error("invalid_v2_plan", "bad output path")
        outputs.append({"name": o["name"], "path": path, "required": bool(o.get("required", True))})
    egress = [str(e) for e in v2.get("egress") or []]
    for e in egress:
        if not _EGRESS.match(e) or not 0 < int(e.rsplit(":", 1)[1]) < 65536:
            raise V2Error("invalid_v2_plan", "bad egress entry")  # exact host:port only, no wildcards
    ck = v2.get("checkpoint") or {}
    ck_dir = ck.get("dir") if isinstance(ck, dict) else None
    if ck_dir is not None:
        try:
            check_mount_target(str(ck_dir))
        except ExecutorError as exc:
            raise V2Error("invalid_v2_plan", str(exc))
    interval = max(int(min_interval), int(ck.get("interval_seconds") or 0)) if ck_dir else 0
    resume = ck.get("resume") if isinstance(ck, dict) else None
    if resume is not None and not (isinstance(resume, dict) and _SHA.match(str(resume.get("sha256", ""))) and resume.get("file_id")):
        raise V2Error("invalid_v2_plan", "bad resume")
    return Plan(slug, inputs, assets, outputs, egress, str(ck_dir) if ck_dir else None, interval, resume)


# --------------------------------------------------------------------------- #
# Hashing and verified download
# --------------------------------------------------------------------------- #
def hash_file(path: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def download_verified(
    url: str,
    dest: str,
    *,
    sha256: str,
    size: int,
    transport: Any = None,
    timeout: float = 300.0,
    on_chunk: Optional[Callable[[int], None]] = None,
    retries: int = 3,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Stream ``url`` to ``dest`` (never an API URL; a presigned store URL), checking size and SHA-256.

    Returns the bytes downloaded. On mismatch the partial file is deleted and ``input_hash_mismatch`` raised
    (not retried: a wrong hash is not a network blip). Network errors are retried with a short backoff.
    """
    import httpx

    part = dest + ".part"
    last: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        h, got = hashlib.sha256(), 0
        try:
            kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
            if transport is not None:
                kwargs["transport"] = transport
            with httpx.Client(**kwargs) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise V2Error("download_failed", f"HTTP {resp.status_code}")
                    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as fh:
                        for chunk in resp.iter_bytes(CHUNK):
                            got += len(chunk)
                            if got > size:
                                raise V2Error("input_hash_mismatch", "more bytes than declared")
                            h.update(chunk)
                            fh.write(chunk)
                            if on_chunk is not None:
                                on_chunk(len(chunk))
        except V2Error as exc:
            _rm(part)
            if exc.code == "input_hash_mismatch":
                raise
            last = exc
        except (httpx.HTTPError, OSError) as exc:
            _rm(part)
            last = exc
        else:
            if got != size or h.hexdigest() != sha256:
                _rm(part)
                raise V2Error("input_hash_mismatch", "downloaded bytes do not match the declared size or SHA-256")
            os.replace(part, dest)
            return got
        if attempt + 1 < retries:
            sleeper(min(2.0 ** attempt, 10.0))
    raise V2Error("download_failed", f"download failed: {type(last).__name__}")


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Per-kit, content-addressed, LRU host cache
# --------------------------------------------------------------------------- #
class ContentCache:
    """``<root>/<kit>/<sha[:2]>/<sha>``. LRU by file mtime (touched on every hit). Entries pinned by a running
    job are never evicted. ``max_bytes == 0`` disables the cache. Counters back the U3.2 "network counter" proof."""

    def __init__(self, root: str, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.downloads = 0
        self.bytes_downloaded = 0
        self.hits = 0
        self._pins: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0

    def _path(self, kit: str, sha: str) -> Path:
        if not _KIT.match(kit) or not _SHA.match(sha):
            raise V2Error("invalid_v2_plan", "bad cache key")
        return self.root / kit / sha[:2] / sha

    def lookup(self, kit: str, sha: str, size: int) -> Optional[Path]:
        if not self.enabled:
            return None
        path = self._path(kit, sha)
        try:
            st = path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            _rm(str(path))  # damaged or replaced entry: forget it
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        with self._lock:
            self.hits += 1
        return path

    def _entries(self) -> list[tuple[float, int, Path]]:
        out: list[tuple[float, int, Path]] = []
        if not self.root.exists():
            return out
        for p in self.root.glob("*/*/*"):
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and _SHA.match(p.name):
                out.append((st.st_mtime, st.st_size, p))
        return out

    def total_bytes(self) -> int:
        return sum(size for _, size, _ in self._entries())

    def pin(self, path: Path) -> None:
        with self._lock:
            self._pins[str(path)] = self._pins.get(str(path), 0) + 1

    def unpin(self, path: Path) -> None:
        with self._lock:
            n = self._pins.get(str(path), 0) - 1
            if n <= 0:
                self._pins.pop(str(path), None)
            else:
                self._pins[str(path)] = n

    def _evict_for(self, needed: int) -> bool:
        """Free space (oldest first, skipping pinned entries) so ``needed`` more bytes fit; False if impossible."""
        entries = sorted(self._entries(), key=lambda e: e[0])
        used = sum(e[1] for e in entries)
        with self._lock:
            pinned = set(self._pins)
        for _mtime, size, path in entries:
            if used + needed <= self.max_bytes:
                break
            if str(path) in pinned:
                continue
            _rm(str(path))
            used -= size
        return used + needed <= self.max_bytes

    def store(self, kit: str, sha: str, src: str, size: int) -> Optional[Path]:
        """Move a VERIFIED file into the cache and return its path, or None if it cannot fit (caller keeps ``src``)."""
        if not self.enabled or size > self.max_bytes:
            return None
        path = self._path(kit, sha)
        if not self._evict_for(size):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        get_platform().paths.set_mode(src, 0o444)
        try:
            os.replace(src, path)
        except OSError:  # different filesystem: copy beside the target, then rename atomically
            tmp = str(path) + ".tmp"
            shutil.copyfile(src, tmp)
            get_platform().paths.set_mode(tmp, 0o444)
            os.replace(tmp, path)
            _rm(src)
        return path


# --------------------------------------------------------------------------- #
# Server calls (machine-signed, JSON)
# --------------------------------------------------------------------------- #
class V2Api:
    def __init__(self, base_url: str, machine_id: str, machine_secret: str, *, transport: Any = None, timeout: float = 60.0) -> None:
        self.base_url, self.machine_id, self.machine_secret = base_url.rstrip("/"), machine_id, machine_secret
        self.transport, self.timeout = transport, timeout

    def post(self, path: str, body: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
        from .jobrunner import _signed_post  # late import: jobrunner imports this module

        resp = _signed_post(
            f"{self.base_url}/v1/agents/v2{path}", self.machine_id, self.machine_secret, body or {},
            transport=self.transport, timeout=self.timeout,
        )
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {}

    def ok(self, path: str, body: Optional[dict[str, Any]] = None, *, code: str) -> dict[str, Any]:
        status, data = self.post(path, body)
        if status != 200:
            raise V2Error(code, f"{path}: HTTP {status} {data.get('detail', '')}")
        return data


def upload_file(
    api: V2Api,
    job_id: str,
    path: str,
    *,
    purpose: str,
    name: str = "",
    seq: Optional[int] = None,
    content_type: str = "application/octet-stream",
    transport: Any = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Upload one file through the data plane (presigned parts straight to the store); return its descriptor."""
    import httpx

    sha, size = hash_file(path)
    base = f"/jobs/{job_id}/uploads"
    body: dict[str, Any] = {"purpose": purpose, "sha256": sha, "size_bytes": size, "content_type": content_type}
    if purpose == "output":
        body["name"] = name
    else:
        body["seq"] = seq
    st = api.ok(base, body, code="upload_failed")
    uid = st["upload_id"]
    if st.get("status") != "complete":
        part_size, part_count, done = int(st["part_size"]), int(st["part_count"]), set(st.get("verified_parts") or [])
        with open(path, "rb") as fh:
            for n in range(1, part_count + 1):
                if n in done:
                    continue
                fh.seek((n - 1) * part_size)
                data = fh.read(part_size)
                psha = hashlib.sha256(data).hexdigest()
                for _attempt in range(3):
                    out = api.ok(f"{base}/{uid}/parts/{n}/url", {"sha256": psha, "size_bytes": len(data)}, code="upload_failed")
                    if out.get("already_uploaded"):
                        break
                    kwargs: dict[str, Any] = {"timeout": 600.0}
                    if transport is not None:
                        kwargs["transport"] = transport
                    with httpx.Client(**kwargs) as client:  # straight to the store, no machine auth header
                        resp = client.put(out["url"], content=data, headers=out.get("headers") or {})
                    if resp.status_code not in (200, 201, 204):
                        sleeper(1.0)
                        continue
                    status, _ = api.post(f"{base}/{uid}/parts/{n}/confirm")
                    if status == 200:
                        break
                    if status != 409:  # 409 = the store holds a corrupt part: send it again
                        raise V2Error("upload_failed", f"confirm HTTP {status}")
                else:
                    raise V2Error("upload_failed", f"part {n} could not be stored intact")
        done_info: dict[str, Any] = {}
        for _ in range(120):  # large files may be proven by the worker: poll, bounded
            done_info = api.ok(f"{base}/{uid}/complete", code="upload_failed")
            if done_info.get("file_id"):
                break
            sleeper(5.0)
        file_id = done_info.get("file_id")
    else:
        file_id = st.get("file_id")
    if not file_id:
        raise V2Error("upload_not_verified", "the store did not confirm the whole-file hash")
    return {"kind": purpose, "name": name or f"ckpt-{seq}", "sha256": sha, "size": size, "content_type": content_type, "file_id": str(file_id)}


# --------------------------------------------------------------------------- #
# Checkpoint packing and safe restore
# --------------------------------------------------------------------------- #
def pack_dir(src: str, dest_tar: str) -> None:
    """Deterministic tar of regular files and dirs only (links and specials skipped).

    The source dir is written by an untrusted container that may still be running (checkpoints): the walk is
    descriptor-based (``os.fwalk``) and every file is opened with ``O_NOFOLLOW`` relative to its directory fd and
    checked with ``fstat`` on the opened descriptor, so a file swapped for a symlink between listing and reading can
    never make the agent read (and upload) a host file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    with tarfile.open(dest_tar, "w", format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files, dirfd in os.fwalk(src, follow_symlinks=False):
            dirs.sort()
            for fname in sorted(files):
                try:
                    fd = os.open(fname, flags, dir_fd=dirfd)
                except OSError:
                    continue  # a symlink (ELOOP) or a vanished file
                with os.fdopen(fd, "rb") as fh:
                    if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                        continue
                    arcname = os.path.relpath(os.path.join(root, fname), src)
                    info = tar.gettarinfo(arcname=arcname, fileobj=fh)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    info.mode = 0o644
                    tar.addfile(info, fh)


def safe_extract(tar_path: str, dest: str, *, max_bytes: int) -> None:
    """Extract by hand: regular files and directories only, no absolute or ``..`` names, size capped."""
    root = os.path.realpath(dest)
    total = 0
    with tarfile.open(tar_path, "r") as tar:
        for member in tar:
            name = member.name
            if name.startswith("/") or "\x00" in name or ".." in name.split("/"):
                raise V2Error("unsafe_checkpoint", "unsafe member name")
            if not (member.isfile() or member.isdir()):
                raise V2Error("unsafe_checkpoint", "only files and directories are allowed")
            target = os.path.realpath(os.path.join(root, name))
            if target != root and not target.startswith(root + os.sep):
                raise V2Error("unsafe_checkpoint", "member escapes the checkpoint dir")
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                get_platform().paths.set_mode(target, 0o777)
                continue
            total += member.size
            if total > max_bytes:
                raise V2Error("unsafe_checkpoint", "checkpoint larger than the limit")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tar.extractfile(member)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o666)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            get_platform().paths.set_mode(target, 0o666)


def _newest_mtime(path: str) -> float:
    newest = 0.0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                newest = max(newest, os.lstat(os.path.join(root, f)).st_mtime)
            except OSError:
                pass
    return newest


class CheckpointUploader(threading.Thread):
    """Every ``interval`` seconds seal the checkpoint dir (only when the Kit wrote its COMMIT marker and the
    dir has been quiet) and upload it. Failures are logged, never fatal."""

    def __init__(self, api: V2Api, job_id: str, ckpt_dir: str, scratch: str, interval: float, *, first_seq: int,
                 max_bytes: int, stop: threading.Event, logs: list[str], transport: Any = None,
                 clock: Callable[[], float] = time.time, sleeper: Callable[[float], None] = time.sleep,
                 heartbeat: Optional[Callable[[], None]] = None) -> None:
        super().__init__(daemon=True)
        self.api, self.job_id, self.ckpt_dir, self.scratch = api, job_id, ckpt_dir, scratch
        self.interval, self.seq, self.max_bytes, self.stop_event = interval, first_seq, max_bytes, stop
        self.logs, self.transport, self.clock, self.sleeper, self.heartbeat = logs, transport, clock, sleeper, heartbeat
        self.last_sha: Optional[str] = None
        self.uploaded = 0

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.snapshot()

    def snapshot(self) -> bool:
        try:
            if self.heartbeat is not None:
                self.heartbeat()
            if not os.path.exists(os.path.join(self.ckpt_dir, COMMIT_MARKER)):
                return False
            if self.clock() - _newest_mtime(self.ckpt_dir) < QUIET_SECONDS:
                return False  # still being written: try next tick, never a torn snapshot
            tar_path = os.path.join(self.scratch, f"ckpt-{self.seq}.tar")
            pack_dir(self.ckpt_dir, tar_path)
            try:
                sha, size = hash_file(tar_path)
                if size > self.max_bytes:
                    self.logs.append("[checkpoint] skipped: larger than checkpoint_max_bytes")
                    return False
                if sha == self.last_sha:
                    return False  # nothing changed since the last proven checkpoint
                upload_file(self.api, self.job_id, tar_path, purpose="checkpoint", seq=self.seq,
                            content_type="application/x-tar", transport=self.transport, sleeper=self.sleeper)
                self.last_sha = sha
                self.seq += 1
                self.uploaded += 1
                self.logs.append(f"[checkpoint] uploaded seq {self.seq - 1} ({size} bytes)")
                return True
            finally:
                _rm(tar_path)
        except Exception as exc:  # a failed checkpoint must never fail the job
            self.logs.append(f"[checkpoint] upload failed: {type(exc).__name__}")
            return False


def _seq_of(name: str) -> int:
    m = re.search(r"/(\d{8})\.tar$", name or "")
    return int(m.group(1)) if m else -1


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class V2Context:
    api: V2Api
    cache: ContentCache
    work_dir: Optional[str] = None
    checkpoint_min_interval: int = 60
    checkpoint_max_bytes: int = 10 * 1024**3
    transport: Any = None
    sleeper: Callable[[float], None] = time.sleep
    heartbeat: Optional[Callable[[], None]] = None


def context_from_config(config: Any, api: V2Api, **kw: Any) -> V2Context:
    cache_dir = config.cache_dir or str(get_platform().paths.cache_dir())
    return V2Context(
        api=api,
        cache=ContentCache(cache_dir, config.cache_max_bytes),
        work_dir=config.work_dir,
        checkpoint_min_interval=config.checkpoint_min_interval_seconds,
        checkpoint_max_bytes=config.checkpoint_max_bytes,
        **kw,
    )


def _failed(spec: JobSpec, code: str, message: str, logs: list[str]) -> ExecutionResult:
    return ExecutionResult(
        status="failed", execution_mode=spec.execution_mode,
        metrics={"duration_seconds": 0.0, "max_memory_mb": None, "gpu_util_pct": None},
        logs=logs + [f"[v2] {message}"], output_schema_valid=False, error={"code": code, "message": message},
    )


def _fetch(ctx: V2Context, job_id: str, kind: str, name: str, dest: str, sha: str, size: int) -> int:
    info = ctx.api.ok(f"/jobs/{job_id}/download-url", {"kind": kind, "name": name}, code="download_url_failed")
    last = [0.0]

    def tick(_n: int) -> None:
        if ctx.heartbeat is not None and time.monotonic() - last[0] > 30:
            last[0] = time.monotonic()
            try:
                ctx.heartbeat()
            except Exception:
                pass

    return download_verified(info["url"], dest, sha256=sha, size=size, transport=ctx.transport, on_chunk=tick, sleeper=ctx.sleeper)


def stage_items(ctx: V2Context, job_id: str, plan: Plan, jobdir: str, pins: list[Path], logs: list[str]) -> list[dict[str, Any]]:
    """Download (or reuse from the cache), verify and describe the read-only mounts for inputs and assets."""
    mounts: list[dict[str, Any]] = []
    in_dir = os.path.join(jobdir, "in")
    for kind, items, target_root in (("asset", plan.assets, CACHE_TARGET), ("input", plan.inputs, IN_TARGET)):
        for item in items:
            name, sha, size = item["name"], item["sha256"], item["size_bytes"]
            source: Optional[Path] = None
            if item["cacheable"]:
                source = ctx.cache.lookup(plan.kit, sha, size)
                if source is not None:
                    logs.append(f"[cache] hit {kind} {name} (no download)")
            if source is None:
                free = shutil.disk_usage(jobdir).free
                if size > free:
                    raise V2Error("insufficient_disk", f"{kind} {name} needs {size} bytes, {free} free")
                # Staged under a kind-prefixed name: an asset and an input (or "x" and "x.dl") may share a name, and
                # the same file name used to let one silently replace the other when the cache did not take it.
                tmp = os.path.join(in_dir, f"{kind}-{name}.dl")
                got = _fetch(ctx, job_id, kind, name, tmp, sha, size)
                ctx.cache.downloads += 1
                ctx.cache.bytes_downloaded += got
                logs.append(f"[stage] downloaded {kind} {name} ({got} bytes, verified)")
                if item["cacheable"]:
                    source = ctx.cache.store(plan.kit, sha, tmp, size)
                if source is None:
                    final = os.path.join(in_dir, f"{kind}-{name}")
                    get_platform().paths.set_mode(tmp, 0o444)
                    os.replace(tmp, final)
                    source = Path(final)
            if str(source).startswith(str(ctx.cache.root)):
                ctx.cache.pin(source)
                pins.append(source)
            mounts.append({"source": str(source), "target": f"{target_root}/{name}", "readonly": True})
    return mounts


def restore_checkpoint(ctx: V2Context, job_id: str, plan: Plan, jobdir: str, ckpt_dir: str, logs: list[str]) -> bool:
    if plan.resume is None or plan.checkpoint_dir is None:
        return False
    r = plan.resume
    tar_path = os.path.join(jobdir, "resume.tar")
    try:
        _fetch(ctx, job_id, "checkpoint", str(r["file_id"]), tar_path, r["sha256"], int(r.get("size_bytes") or 0))
        safe_extract(tar_path, ckpt_dir, max_bytes=ctx.checkpoint_max_bytes)
        logs.append(f"[checkpoint] resumed from {r.get('name')}")
        return True
    except V2Error as exc:  # start from scratch rather than run on a damaged or unsafe checkpoint
        shutil.rmtree(ckpt_dir, ignore_errors=True)
        os.makedirs(ckpt_dir, exist_ok=True)
        get_platform().paths.set_mode(ckpt_dir, 0o777)
        logs.append(f"[checkpoint] could not resume ({exc.code}); starting from scratch")
        return False
    finally:
        _rm(tar_path)


def collect_and_upload_outputs(ctx: V2Context, job_id: str, plan: Plan, out_dir: str, jobdir: str) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    root = os.path.realpath(out_dir)
    for o in plan.outputs:
        path = os.path.join(root, o["path"])
        real = os.path.realpath(path)
        if not (real == root or real.startswith(root + os.sep)) or os.path.islink(path):
            raise V2Error("unsafe_output", f"output {o['name']} escapes the output dir")
        if not os.path.exists(real):
            if o["required"]:
                raise V2Error("missing_output", f"required output {o['name']} was not produced")
            continue
        upload_path, ctype, tmp_tar = real, "application/octet-stream", None
        if os.path.isdir(real):
            tmp_tar = os.path.join(jobdir, f"out-{o['name']}.tar")
            pack_dir(real, tmp_tar)
            upload_path, ctype = tmp_tar, "application/x-tar"
        elif not os.path.isfile(real):
            raise V2Error("unsafe_output", f"output {o['name']} is not a regular file")
        try:
            descriptors.append(upload_file(ctx.api, job_id, upload_path, purpose="output", name=o["name"], content_type=ctype,
                                           transport=ctx.transport, sleeper=ctx.sleeper))
        finally:
            if tmp_tar:
                _rm(tmp_tar)
    return descriptors


def run_v2(
    spec: JobSpec,
    ctx: V2Context,
    run_executor: Callable[[JobSpec], ExecutionResult],
) -> tuple[ExecutionResult, list[dict[str, Any]]]:
    """Stage, run, checkpoint and upload one v2 job. Returns the result and the output descriptors for the receipt."""
    logs: list[str] = []
    if (spec.isolation_tier or "container").lower() in PRIVATE_TIERS:
        return _failed(spec, "v2_not_supported_for_privacy_tier", "v2 staging is refused for privacy tiers", logs), []
    try:
        plan = parse_plan(spec.v2 or {}, min_interval=ctx.checkpoint_min_interval)
    except V2Error as exc:
        return _failed(spec, exc.code, str(exc), logs), []

    pins: list[Path] = []
    base = ctx.work_dir or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    jobdir = tempfile.mkdtemp(prefix=f"ccoin-v2-{spec.job_id[:8]}-", dir=base)
    out_dir, ckpt_dir, in_dir = (os.path.join(jobdir, d) for d in ("out", "ckpt", "in"))
    stop = threading.Event()
    uploader: Optional[CheckpointUploader] = None
    try:
        for d, mode in ((out_dir, 0o777), (ckpt_dir, 0o777), (in_dir, 0o700)):
            os.makedirs(d, exist_ok=True)
            get_platform().paths.set_mode(d, mode)
        try:
            mounts = stage_items(ctx, spec.job_id, plan, jobdir, pins, logs)
            env = {}
            if plan.inputs:
                env["COMPUTECOIN_INPUTS_DIR"] = IN_TARGET
            if plan.assets:
                env["COMPUTECOIN_CACHE_DIR"] = CACHE_TARGET
            first_seq = 0
            if plan.checkpoint_dir:
                resumed = restore_checkpoint(ctx, spec.job_id, plan, jobdir, ckpt_dir, logs)
                mounts.append({"source": ckpt_dir, "target": plan.checkpoint_dir, "readonly": False})
                env["COMPUTECOIN_CHECKPOINT_DIR"] = plan.checkpoint_dir
                if resumed:
                    env["COMPUTECOIN_RESUME"] = "1"
                    first_seq = _seq_of(str((plan.resume or {}).get("name"))) + 1
        except V2Error as exc:
            return _failed(spec, exc.code, str(exc), logs), []

        spec.mounts, spec.extra_env, spec.output_dir = mounts, env, out_dir
        if plan.egress:  # Kit-declared egress rides the existing B5 allowlist (never for privacy tiers, checked above)
            existing = list((spec.network_policy or {}).get("egress") or [])
            spec.network_policy = {"egress": sorted(set(existing) | set(plan.egress))}

        if plan.checkpoint_dir:
            uploader = CheckpointUploader(
                ctx.api, spec.job_id, ckpt_dir, jobdir, float(plan.checkpoint_interval), first_seq=first_seq,
                max_bytes=ctx.checkpoint_max_bytes, stop=stop, logs=logs, transport=ctx.transport,
                sleeper=ctx.sleeper, heartbeat=ctx.heartbeat,
            )
            uploader.start()
        try:
            result = run_executor(spec)
        finally:
            stop.set()
            if uploader is not None:
                uploader.join(timeout=30)
        result.logs = logs + list(result.logs)
        result.metrics = {
            **(result.metrics or {}),
            "cache_hits": ctx.cache.hits, "cache_downloads": ctx.cache.downloads,
            "bytes_downloaded": ctx.cache.bytes_downloaded,
            "checkpoints_uploaded": uploader.uploaded if uploader else 0,
        }
        if result.status != "success":
            return result, []
        try:
            descriptors = collect_and_upload_outputs(ctx, spec.job_id, plan, out_dir, jobdir)
        except V2Error as exc:
            failed = _failed(spec, exc.code, str(exc), list(result.logs))
            failed.metrics = result.metrics
            return failed, []
        result.output_schema_valid = bool(descriptors) or not plan.outputs
        return result, descriptors
    finally:
        stop.set()
        for p in pins:
            ctx.cache.unpin(p)
        shutil.rmtree(jobdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# U14 Windows VM jobs (default off). Design: docs/architecture/U14-windows-vm-design.md
# --------------------------------------------------------------------------- #
VM_IMAGE_NAME = "vm-image"
VM_MAX_COMMAND_ARGS = 32


def _valid_vm_command(cmd: Any) -> Optional[list[str]]:
    if isinstance(cmd, list) and 0 < len(cmd) <= VM_MAX_COMMAND_ARGS and all(isinstance(c, str) and 0 < len(c) <= 512 for c in cmd):
        return list(cmd)
    return None


def _vm_command(vm: Any) -> Optional[list[str]]:
    """The VM command, run only inside the customer's own VM. Primary source: the ``vm`` block's ``command`` field.
    DEPRECATED fallback (one version, then removed): ``params.vm_command``."""
    if not isinstance(vm, dict):
        return None
    cmd = _valid_vm_command(vm.get("command"))
    if cmd is not None:
        return cmd
    params = vm.get("params")
    return _valid_vm_command(params.get("vm_command") if isinstance(params, dict) else None)


def run_vm_v2(
    spec: JobSpec,
    ctx: V2Context,
    *,
    windows_vm_enabled: bool,
    vm_module: Any = None,
) -> tuple[ExecutionResult, list[dict[str, Any]]]:
    """Run a claim whose v2 block says ``mode: "vm"`` through ``vm_runner.run_vm_job``. Fail closed everywhere.

    Refused (never run as an ordinary Kit job) unless ``windows_vm_enabled`` is true on this agent AND the host really
    advertises ``vfio_gpu`` (when GPU passthrough is asked), and never on a privacy tier or with egress or a checkpoint.
    The customer's image and inputs are staged and hash-verified like any v2 input, copied into one directory, and the
    guest gets them on a read-only disk (see provider-agent/vm/guest/run-job.ps1 for the guest side).
    """
    logs: list[str] = []
    v2 = spec.v2 or {}
    if (spec.isolation_tier or "container").lower() in PRIVATE_TIERS:
        return _failed(spec, "v2_not_supported_for_privacy_tier", "a VM job is refused on privacy tiers", logs), []
    if not windows_vm_enabled:
        return _failed(spec, "windows_vm_disabled", "this host has not enabled the Windows VM tier", logs), []
    if vm_module is None:
        from . import vm_runner as vm_module  # type: ignore[no-redef]
    vm = v2.get("vm")
    image = vm.get("image") if isinstance(vm, dict) else None
    if not isinstance(vm, dict) or not isinstance(image, dict) or not _SHA.match(str(image.get("sha256", ""))) \
            or not isinstance(image.get("size_bytes"), int) or image["size_bytes"] <= 0:
        return _failed(spec, "vm_spec_unavailable", "the claim carries no usable VM image", logs), []
    passthrough = bool(vm.get("gpu_passthrough", True))
    gpu_addrs: list[str] = []
    if passthrough:
        if vm_module.advertise().get("vfio_gpu") is not True:
            return _failed(spec, "vfio_gpu_required", "this host does not advertise vfio_gpu", logs), []
        gpu_addrs = list(vm_module.detect_vfio_gpu())[: max(1, int((spec.limits or {}).get("gpu_count") or 1))]
    try:
        plan = parse_plan(v2, min_interval=ctx.checkpoint_min_interval)
        vcpus, mem = int(vm.get("vcpus", 4)), int(vm.get("memory_gb", 16))
        if not (1 <= vcpus <= 64 and 2 <= mem <= 512):
            raise V2Error("invalid_v2_plan", "bad VM size")
        if plan.egress or plan.checkpoint_dir:
            raise V2Error("invalid_v2_plan", "a VM job has no network and no checkpoint")
    except (V2Error, ValueError, TypeError) as exc:
        return _failed(spec, getattr(exc, "code", "invalid_v2_plan"), str(exc), logs), []

    pins: list[Path] = []
    base = ctx.work_dir or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    jobdir = tempfile.mkdtemp(prefix=f"ccoin-vm-{spec.job_id[:8]}-", dir=base)
    get_platform().paths.set_mode(jobdir, 0o700)
    out_dir, vm_in = os.path.join(jobdir, "out"), os.path.join(jobdir, "vm_in")
    try:
        for d in (out_dir, vm_in, os.path.join(jobdir, "in")):
            os.makedirs(d, exist_ok=True)
        try:
            free = shutil.disk_usage(jobdir).free
            if image["size_bytes"] * 2 > free:  # image plus its copy-on-write overlay headroom
                raise V2Error("insufficient_disk", "not enough free disk for the VM image")
            image_path = os.path.join(jobdir, "in", VM_IMAGE_NAME)
            _fetch(ctx, spec.job_id, "input", VM_IMAGE_NAME, image_path, image["sha256"], image["size_bytes"])
            logs.append("[vm] image downloaded and verified")
            for m in stage_items(ctx, spec.job_id, plan, jobdir, pins, logs):
                if m["target"].startswith(f"{IN_TARGET}/"):
                    shutil.copyfile(m["source"], os.path.join(vm_in, m["target"][len(IN_TARGET) + 1:]))
            import json as _json

            with open(os.path.join(vm_in, "job.json"), "w", encoding="utf-8") as fh:
                _json.dump({"job_id": spec.job_id, "command": _vm_command(vm), "params": vm.get("params") or {}},
                           fh, sort_keys=True)
        except V2Error as exc:
            return _failed(spec, exc.code, str(exc), logs), []

        timeout = int((spec.limits or {}).get("max_runtime_seconds") or 3600)
        job = vm_module.VmJob(
            job_id=spec.job_id, image_path=image_path, image_sha256=image["sha256"], input_dir=vm_in,
            vcpus=vcpus, memory_gb=mem, gpu_addresses=gpu_addrs, timeout_seconds=timeout,
            control_payload={"job_file": "job.json"},
        )
        vres = vm_module.run_vm_job(job, work_root=jobdir, out_dir=out_dir)
        logs.extend(f"[vm] {line}" for line in (vres.progress or [])[:50])
        metrics = {"duration_seconds": round(float(vres.duration_seconds or 0.0), 3), "max_memory_mb": None, "gpu_util_pct": None}
        if vres.status != "success":
            failed = _failed(spec, vres.error_code or "vm_failed", f"the VM job ended with {vres.status}", logs)
            failed.metrics = metrics
            return failed, []
        try:
            descriptors = collect_and_upload_outputs(ctx, spec.job_id, plan, out_dir, jobdir)
        except V2Error as exc:
            failed = _failed(spec, exc.code, str(exc), logs)
            failed.metrics = metrics
            return failed, []
        res = ExecutionResult(status="success", output={}, execution_mode=spec.execution_mode, metrics=metrics, logs=logs,
                              output_schema_valid=bool(descriptors) or not plan.outputs)
        return res, descriptors
    finally:
        for p in pins:
            ctx.cache.unpin(p)
        shutil.rmtree(jobdir, ignore_errors=True)
