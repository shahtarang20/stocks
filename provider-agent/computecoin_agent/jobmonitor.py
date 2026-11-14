"""Per-job sidecar: signed progress/GPU telemetry (U19) and ordered token streaming (B2).

One daemon thread runs next to the executor for a non-private job. It never raises into the job
and never blocks the executor; everything it does is best-effort.

File contract (all inside the job's output directory, which the container sees as /out):

* ``stream.ndjson``  one JSON object per line, appended by the workload as it generates:
  ``{"text": "<next piece of generated text>", "final": false}``. The agent numbers the chunks
  (seq 0, 1, 2, ...) in file order and POSTs them to ``/v1/agents/jobs/{job_id}/stream``. The
  last line SHOULD carry ``"final": true`` (it may have empty text); if the workload ends without
  one the agent sends an empty final chunk. A partial last line is never read until its newline.
* ``progress.json``  optional, replaced atomically by the workload:
  ``{"progress_pct": 0-100, "step_count": int, "phase": str}``. Without it the agent reports
  0 % with phase ``running`` (it never invents a percentage); GPU util and VRAM are real samples.

Private jobs (``seal_to`` set, U18): ``stream.ndjson`` is a named pipe (FIFO) created inside the job's output dir, which
the private run places on RAM-only storage, so no plaintext stream is ever written to disk and nothing stays behind
after a read. The monitor reads it in-process, pads each chunk's plaintext to ``PRIVACY_STREAM_PAD_BYTES`` (default 256,
0 = off; framing in :func:`private_kit.pad_stream_plaintext`), seals it and posts only the ciphertext.

Environment given to the workload: ``COMPUTECOIN_STREAM`` and ``COMPUTECOIN_PROGRESS`` (paths).
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from typing import Any, Callable, Optional

from .platform import PlatformUnsupported, get_platform

STREAM_FILE = "stream.ndjson"
PROGRESS_FILE = "progress.json"
MAX_READ_BYTES = 8 * 1024 * 1024  # per read of a job-written file (stream / progress): the job controls these files


def _read_job_file_from(path: str, offset: int, limit: int) -> bytes:
    """Read at most ``limit`` bytes of a file the untrusted job wrote, from ``offset``. A symlink (host-file read), a
    FIFO or any non-regular file is refused with OSError (O_NOFOLLOW, fstat)."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError("not a regular file")
        fh.seek(offset)
        return fh.read(limit)


MAX_CHUNK_CHARS = 4000  # the API accepts up to 8192; stay well under
BYOC_SAMPLE_SECONDS = 15.0  # U16: how often a BYOC container is sampled (docker top / stats are not free)
BYOC_MAX_PROCESSES = 64  # report at most this many process names
BYOC_MAX_NAME_CHARS = 64
BYOC_DOCKER_TIMEOUT = 5.0
_SIZE_UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
               "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def _parse_size(text: str) -> int:
    t = text.strip().lower()
    i = len(t)
    while i > 0 and not (t[i - 1].isdigit() or t[i - 1] == "."):
        i -= 1
    try:
        return int(float(t[:i]) * _SIZE_UNITS.get(t[i:].strip() or "b", 1))
    except ValueError:
        return 0


def parse_docker_top(output: str) -> list[str]:
    """``docker top <c> -eo comm`` output -> unique process base names (never arguments or contents), size-capped."""
    names: list[str] = []
    for line in output.splitlines()[1:]:  # first line is the COMMAND header
        name = line.strip().rsplit("/", 1)[-1][:BYOC_MAX_NAME_CHARS]
        if name and name not in names:
            names.append(name)
        if len(names) >= BYOC_MAX_PROCESSES:
            break
    return names


def parse_docker_stats_io(output: str) -> Optional[int]:
    """``docker stats --no-stream --format '{{.NetIO}}|{{.BlockIO}}'`` -> cumulative bytes (net + block), or None."""
    total, seen = 0, False
    for part in output.strip().splitlines()[:1]:
        for pair in part.split("|"):
            for half in pair.split("/"):
                if half.strip():
                    total += _parse_size(half)
                    seen = True
    return total if seen else None


def _docker_out(argv: list[str]) -> Optional[str]:  # pragma: no cover - needs docker
    import subprocess

    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=BYOC_DOCKER_TIMEOUT, check=False,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout[:65536] if r.returncode == 0 else None


def _gpu_point() -> dict[str, Any]:
    """Real GPU util / VRAM of GPU 0, or {} (the simulated probe fallback is never reported)."""
    try:
        from .telemetry import sample_gpu_metrics

        for s in sample_gpu_metrics():
            if (getattr(s, "extra_metrics", None) or {}).get("mode") == "simulated_probe":
                continue
            return {
                "gpu_util_pct": s.gpu_util_pct,
                "mem_used_mb": int(s.mem_used_mb) if s.mem_used_mb is not None else None,
                "mem_total_mb": int(s.mem_total_mb) if s.mem_total_mb is not None else None,
                "power_w": getattr(s, "power_w", None),  # U16: BYOC identical-power signal; ignored elsewhere
            }
    except Exception:
        pass
    return {}


class JobMonitor:
    def __init__(
        self,
        base_url: str,
        machine_id: str,
        machine_secret: str,
        job_id: str,
        *,
        stream: bool = False,
        transport: Any = None,
        interval: float = 2.0,
        gpu_sampler: Callable[[], dict[str, Any]] = _gpu_point,
        timeout: float = 5.0,
        seal_to: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        cancel_poll_seconds: float = 10.0,
        pad_bytes: Optional[int] = None,
        byoc_container: Optional[str] = None,
        docker_runner: Optional[Callable[[list[str]], Optional[str]]] = None,
        shared_gpu_probe: Optional[Callable[[], Optional[list]]] = None,
        sharing_poll_seconds: float = 5.0,
    ) -> None:
        from .progress import ProgressReporter

        self.job_id = job_id
        self.base_url = base_url
        self.machine_id = machine_id
        self.machine_secret = machine_secret
        self.stream = stream
        # U18: a private job. Chunks are sealed to this X25519 key and nothing else is reported (no progress, no GPU).
        self.seal_to = seal_to
        self._sealed: dict[int, dict] = {}
        if pad_bytes is None:
            try:
                pad_bytes = int(os.environ.get("PRIVACY_STREAM_PAD_BYTES", "256"))
            except ValueError:
                pad_bytes = 256  # a bad value never turns padding off silently
        self.pad_bytes = max(0, int(pad_bytes))
        self._fifo_fd: Optional[int] = None
        self._fifo_buf = b""
        # U13: the job heartbeat answers 409 once the API no longer has this job RUNNING (cancelled, failed by a
        # sibling rank, timed out). The monitor then sets this event and the executor stops the container.
        self.cancel_event = cancel_event
        self._cancel_poll = cancel_poll_seconds
        self._last_cancel_poll = time.monotonic()
        # Is the GPU shared with the host owner's own programs while this job runs? ``shared_gpu_probe`` returns the names
        # of other programs on the GPU (``[]`` = looked, none; ``None`` = could not look). Only a yes and a count are ever
        # reported, never a name: those belong to the host's owner, not to the customer.
        self._share_probe = shared_gpu_probe
        self._share_poll = sharing_poll_seconds
        self._last_share_poll = -1e9
        self._share_seen = False
        self._share_max = 0
        # U8/U16: for a BYOC container, sample process NAMES (docker top) and cumulative I/O (docker stats) and report
        # them additively with the progress point. Never contents, never arguments, size capped.
        self.byoc_container = byoc_container
        self._docker_out = docker_runner or _docker_out
        self._last_byoc = -1e9
        self.transport = transport
        self.interval = interval
        self.timeout = timeout
        self._gpu = gpu_sampler
        self._reporter = ProgressReporter(machine_id, machine_secret, base_url, min_interval_seconds=interval)
        self._dir: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._seq = 0
        self._final_sent = False
        self._stream_dead = False
        self._pending: list[tuple[str, bool]] = []

    # -- wiring ----------------------------------------------------------- #
    def set_output_dir(self, path: str) -> None:
        """Called by the executor once the job's output dir exists."""
        self._dir = path
        if self.seal_to:
            self._open_fifo(path)

    @property
    def env(self) -> dict[str, str]:
        return {"COMPUTECOIN_STREAM": f"/out/{STREAM_FILE}", "COMPUTECOIN_PROGRESS": f"/out/{PROGRESS_FILE}"}

    def start(self) -> "JobMonitor":
        self._thread = threading.Thread(target=self._loop, name=f"jobmon-{self.job_id[:8]}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        try:
            self._tick(final=True)
        except Exception:
            pass
        self._close_fifo()

    def __enter__(self) -> "JobMonitor":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- internals -------------------------------------------------------- #
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick(final=False)
            except Exception:
                pass
            self._stop.wait(min(self.interval, 1.0) if self.stream else self.interval)

    def _tick(self, *, final: bool) -> None:
        if self.stream:
            self._pump_stream(final=final)
        if not final and not self.seal_to:
            self._report_progress()
        if not final:
            self._check_cancelled()
            self._observe_sharing()

    def _observe_sharing(self) -> None:
        """Rate-limited, best-effort: remember whether anyone else used the GPU, and how many programs at the most."""
        probe = self._share_probe
        if probe is None or time.monotonic() - self._last_share_poll < self._share_poll:
            return
        self._last_share_poll = time.monotonic()
        try:
            names = probe()
        except Exception:
            return  # a broken probe never stops a job and never invents a signal
        if names:
            self._share_seen = True
            self._share_max = max(self._share_max, min(len(names), 64))

    def sharing_metrics(self) -> dict[str, Any]:
        """``{"gpu_shared": True, "gpu_other_processes": n}`` if another program was seen on the GPU while the job ran,
        else ``{}``. Absence means "not seen", NOT "alone on the GPU": a probe that cannot look (an AMD card, a runtime
        that cannot see the host's processes) also says nothing, and a "not shared" claim would be a guess."""
        if not self._share_seen:
            return {}
        return {"gpu_shared": True, "gpu_other_processes": self._share_max}

    def _check_cancelled(self) -> None:
        ev = self.cancel_event
        if ev is None or ev.is_set() or time.monotonic() - self._last_cancel_poll < self._cancel_poll:
            return
        self._last_cancel_poll = time.monotonic()
        try:
            from .jobrunner import post_heartbeat

            resp = post_heartbeat(self.base_url, self.machine_id, self.machine_secret, self.job_id,
                                  transport=self.transport, timeout=self.timeout)
            if getattr(resp, "status_code", None) == 409:
                ev.set()
        except Exception:
            pass  # best-effort: a network blip never stops a job

    def _byoc_sample(self) -> dict[str, Any]:
        """Rate-limited, best-effort BYOC runtime sample: {process_names, io_bytes} (keys only when obtained)."""
        if not self.byoc_container or time.monotonic() - self._last_byoc < BYOC_SAMPLE_SECONDS:
            return {}
        self._last_byoc = time.monotonic()
        out: dict[str, Any] = {}
        try:
            top = self._docker_out(["docker", "top", self.byoc_container, "-eo", "comm"])
            if top:
                out["process_names"] = parse_docker_top(top)
            stats = self._docker_out(["docker", "stats", "--no-stream", "--format", "{{.NetIO}}|{{.BlockIO}}",
                                      self.byoc_container])
            io = parse_docker_stats_io(stats) if stats else None
            if io is not None:
                out["io_bytes"] = io
        except Exception:
            return {}
        return out

    def _report_progress(self) -> None:
        pct, extra = 0.0, {}
        if self._dir:
            try:
                data = json.loads(_read_job_file_from(os.path.join(self._dir, PROGRESS_FILE), 0, MAX_READ_BYTES).decode("utf-8"))
                if isinstance(data, dict):
                    pct = float(data.get("progress_pct") or 0.0)
                    if pct != pct or pct < 0.0:  # NaN (json.loads accepts it) would clamp to 100 % downstream: never invent a percentage
                        pct = 0.0
                    extra = {"step_count": data.get("step_count"), "phase": data.get("phase")}
            except (OSError, ValueError, TypeError):
                pass
        gpu = self._gpu() or {}
        self._reporter.report(
            self.job_id,
            min(pct, 99.0),  # 100 is reserved for the receipt
            phase=extra.get("phase") or "running",
            gpu_util_pct=gpu.get("gpu_util_pct"),
            mem_used_mb=gpu.get("mem_used_mb"),
            mem_total_mb=gpu.get("mem_total_mb"),
            step_count=extra.get("step_count") if isinstance(extra.get("step_count"), int) else None,
            power_w=gpu.get("power_w") if self.byoc_container else None,
            **self._byoc_sample(),
            transport=self.transport,
            timeout=self.timeout,
        )

    def _open_fifo(self, directory: str) -> None:
        """Private stream: replace any file with a FIFO and hold the read end (non-blocking) before the workload starts."""
        self._close_fifo()
        path = os.path.join(directory, STREAM_FILE)
        try:
            if os.path.lexists(path):
                os.unlink(path)
            fifo = get_platform().fifo
            fifo.create(path, 0o622)
            self._fifo_fd = fifo.open_reader(path)
        except (OSError, PlatformUnsupported):
            self._fifo_fd = None
            self._stream_dead = True  # no RAM pipe, no stream: never fall back to a plaintext file

    def _close_fifo(self) -> None:
        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None
        self._fifo_buf = b""
        self._pending = []  # drop any plaintext still queued

    def _read_fifo_lines(self) -> list[dict[str, Any]]:
        if self._fifo_fd is None:
            return []
        try:
            if not stat.S_ISFIFO(os.fstat(self._fifo_fd).st_mode):
                return []
            while True:
                try:
                    block = os.read(self._fifo_fd, 65536)
                except BlockingIOError:
                    break
                if not block:  # no writer right now (EOF): keep the fd, the workload reopens per write
                    break
                self._fifo_buf += block
                if len(self._fifo_buf) > 8 * 1024 * 1024:
                    self._fifo_buf = b""  # runaway writer: drop rather than grow without bound
                    break
        except OSError:
            return []
        end = self._fifo_buf.rfind(b"\n")
        if end < 0:
            return []
        data, self._fifo_buf = self._fifo_buf[: end + 1], self._fifo_buf[end + 1 :]
        out: list[dict[str, Any]] = []
        for line in data.splitlines():
            try:
                obj = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("text", ""), str):
                out.append(obj)
        return out

    def _read_new_lines(self) -> list[dict[str, Any]]:
        if self.seal_to:
            return self._read_fifo_lines()
        if not self._dir:
            return []
        path = os.path.join(self._dir, STREAM_FILE)
        try:
            data = _read_job_file_from(path, self._offset, MAX_READ_BYTES)
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= MAX_READ_BYTES:
                self._offset += len(data)  # a single runaway line: drop it rather than re-read it forever
            return []
        self._offset += end + 1
        out: list[dict[str, Any]] = []
        for line in data[: end + 1].splitlines():
            try:
                obj = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("text", ""), str):
                out.append(obj)
        return out

    def _pump_stream(self, *, final: bool) -> None:
        if self._stream_dead or self._final_sent:
            return
        for obj in self._read_new_lines():
            text, is_final = obj.get("text", ""), bool(obj.get("final"))
            pieces = [text[i : i + MAX_CHUNK_CHARS] for i in range(0, len(text), MAX_CHUNK_CHARS)] or [""]
            for i, piece in enumerate(pieces):
                if piece or is_final:
                    self._pending.append((piece, is_final and i == len(pieces) - 1))
        if final and (self._pending or self._seq > 0) and not any(f for _, f in self._pending):
            self._pending.append(("", True))
        while self._pending and not self._stream_dead:
            text, is_final = self._pending[0]
            if not self._post_chunk(self._seq, text, is_final):
                break
            self._pending.pop(0)
            self._seq += 1
            if is_final:
                self._final_sent = True
                break

    def _post_chunk(self, seq: int, text: str, final: bool) -> bool:
        from .jobrunner import _signed_post

        body: dict[str, Any] = {"seq": seq, "text": text, "final": final}
        if self.seal_to:
            from .privacy_crypto import seal, stream_aad

            if seq not in self._sealed:  # keep the first ciphertext: a retry must repeat the exact bytes
                from .private_kit import pad_stream_plaintext

                plain = pad_stream_plaintext(text.encode("utf-8"), self.pad_bytes)
                self._sealed[seq] = seal(self.seal_to, plain, stream_aad(self.job_id, seq, final))
            body = {"seq": seq, "text": "", "final": final, "sealed": self._sealed[seq]}
        try:
            r = _signed_post(
                f"{self.base_url.rstrip('/')}/v1/agents/jobs/{self.job_id}/stream",
                self.machine_id, self.machine_secret,
                body,
                transport=self.transport, timeout=self.timeout,
            )
        except Exception:
            return False  # transient: retried next tick (an exact repeat is a harmless retry server side)
        if r.status_code == 200:
            return True
        if r.status_code == 409:
            self._stream_dead = True  # sequence error / not streamable: stop, the job itself is unaffected
        return False
