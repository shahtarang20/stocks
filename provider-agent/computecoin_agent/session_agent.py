"""U10 interactive sessions, host side. The host opens NO port: it connects OUTBOUND to the relay.

Flow: claim a session (machine-signed) -> start a hardened container -> open an outbound WebSocket to the relay
with the single-use host token -> run the end-to-end handshake with the browser -> pump a pty (or a loopback TCP
bridge) in both directions, encrypted. The relay only ever sees the two public keys and ciphertext.

Handshake (text frames, public values only), browser = B, agent = A:
  A has a per-session static X25519 key (sa), published in the claim. B has an ephemeral key (eb), given at create.
  B -> A  {"type":"hello","epk":<eb_pub hex>}
  A -> B  {"type":"hello_ack","epk":<ea_pub hex>}    (ea is a fresh ephemeral per hello, so a reconnect re-keys)
  ikm  = DH(sa, eb) || DH(ea, eb)
  keys = HKDF-SHA256(ikm, salt=sha256(session_id), info="ccsess-v1"||eb_pub||sa_pub||ea_pub, 64 bytes)
         k_b2a = keys[:32], k_a2b = keys[32:]
Data frames are WebSocket BINARY: ChaCha20-Poly1305, nonce = 4 zero bytes || 64-bit big-endian counter per
direction (implicit, strictly increasing; a dropped, replayed or reordered frame fails to decrypt), AAD =
session_id || direction byte. Plaintext = 1 type byte + payload (0 data, 1 resize json, 2 close, 3 ready).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import struct
import tempfile
import subprocess
import time
import uuid
from typing import Any, Callable, Optional, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import interrupts
from . import rental as rental_mod
from .platform import get_platform
from .executor import DockerExecutor, ExecutorError, JobSpec
from .rental import (
    RENTAL_PIDS_LIMIT,
    RentalError,
    RentalRun,
    RentalSettings,
    Workspace,
    assert_rental_argv_safe,
    clean_relpath,
    detect_miner,
    export_results as _export_results,
    list_stale,
    resolve_inside,
    resolve_limits,
)
from .session_http import HttpBridge, open_docker_stream

INFO = b"ccsess-v1"
T_DATA, T_RESIZE, T_CLOSE, T_READY = 0, 1, 2, 3
T_HTTP = 4  # HTTP-over-channel proxy frames (session_http.py, design note section 9)
SESSION_BASE_PATH = "/dashboard/sessions/{sid}/proxy/"  # where the browser serves the app; exported to kits as an env var
MAX_KIT_ARGS = 32
DIR_B2A, DIR_A2B = b"\x00", b"\x01"
MAX_PLAINTEXT = 128 * 1024
HEARTBEAT_SECONDS = 20.0
HEARTBEAT_RETRY_SECONDS = 5.0  # delay before retrying a heartbeat that failed on the network
HEARTBEAT_MAX_FAILURES = 3  # consecutive network failures before the session is treated as lost
AUTOSAVE_SECONDS = 300.0  # a running rental saves changed results to the customer's storage this often (host loss costs at most this much)
MONITOR_SECONDS = 30.0  # how often a rental's disk, heat, owner and miner checks run
INPUT_URL_REFRESH_SECONDS = 300.0  # presigned download links are minted again after this long
_RAW = serialization.Encoding.Raw
_RAW_PUB = serialization.PublicFormat.Raw


class SessionAgentError(RuntimeError):
    pass


def _relay_errors() -> tuple[type[BaseException], ...]:
    """Errors that mean the relay connection dropped (not that the session is over): the caller reconnects."""
    try:
        from websockets.exceptions import WebSocketException
    except Exception:  # websockets is only installed on session hosts
        return ()
    return (WebSocketException,)


def _pub_hex(key: X25519PrivateKey) -> str:
    return key.public_key().public_bytes(_RAW, _RAW_PUB).hex()


def _load_pub(hex32: str) -> X25519PublicKey:
    raw = bytes.fromhex(hex32) if isinstance(hex32, str) else b""
    if len(raw) != 32:
        raise SessionAgentError("bad public key")
    return X25519PublicKey.from_public_bytes(raw)


class SessionCrypto:
    """One handshake's keys and counters. Pure; no I/O."""

    def __init__(self, session_id: str, static_key: X25519PrivateKey, browser_pub_hex: str, ephemeral: Optional[X25519PrivateKey] = None) -> None:
        eb = _load_pub(browser_pub_hex)
        self._ephemeral = ephemeral or X25519PrivateKey.generate()
        sa_pub = static_key.public_key().public_bytes(_RAW, _RAW_PUB)
        ea_pub = self._ephemeral.public_key().public_bytes(_RAW, _RAW_PUB)
        ikm = static_key.exchange(eb) + self._ephemeral.exchange(eb)
        if ikm == b"\x00" * 64:
            raise SessionAgentError("degenerate key exchange")
        okm = HKDF(
            algorithm=hashes.SHA256(), length=64, salt=hashlib.sha256(session_id.encode()).digest(),
            info=INFO + eb.public_bytes(_RAW, _RAW_PUB) + sa_pub + ea_pub,
        ).derive(ikm)
        self._rx = ChaCha20Poly1305(okm[:32])
        self._tx = ChaCha20Poly1305(okm[32:])
        self._aad = session_id.encode()
        self._rx_n = 0
        self._tx_n = 0
        self.ephemeral_pub_hex = ea_pub.hex()

    @staticmethod
    def _nonce(n: int) -> bytes:
        return b"\x00\x00\x00\x00" + struct.pack(">Q", n)

    def seal(self, kind: int, payload: bytes) -> bytes:
        out = self._tx.encrypt(self._nonce(self._tx_n), bytes([kind]) + payload, self._aad + DIR_A2B)
        self._tx_n += 1
        return out

    def open(self, frame: bytes) -> tuple[int, bytes]:
        try:
            plain = self._rx.decrypt(self._nonce(self._rx_n), frame, self._aad + DIR_B2A)
        except Exception as exc:  # InvalidTag: tampered, dropped, replayed or reordered
            raise SessionAgentError("frame failed authentication") from exc
        self._rx_n += 1
        if not plain:
            raise SessionAgentError("empty frame")
        return plain[0], plain[1:]


class Endpoint(Protocol):
    async def read(self) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    def resize(self, rows: int, cols: int) -> None: ...
    async def close(self) -> None: ...


class PtyEndpoint:
    """`docker exec -it` on a host pty. Reads return b"" at EOF."""

    def __init__(self, argv: list[str]) -> None:
        import pty

        self._master, slave = pty.openpty()
        self._proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave)

    async def read(self) -> bytes:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, os.read, self._master, 16384)
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:
        await asyncio.get_running_loop().run_in_executor(None, os.write, self._master, data)

    def resize(self, rows: int, cols: int) -> None:
        import fcntl
        import termios

        with contextlib.suppress(Exception):
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, min(rows, 500)), max(1, min(cols, 500)), 0, 0))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._proc.kill()
        with contextlib.suppress(OSError):
            os.close(self._master)


class TcpBridgeEndpoint:
    """Loopback TCP inside the container, reached with `docker exec -i <container> nc 127.0.0.1 <port>` (the
    container has no network, so the host cannot dial it directly)."""

    def __init__(self, argv: list[str]) -> None:
        self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)

    async def read(self) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(None, self._proc.stdout.read1, 16384)  # type: ignore[union-attr]

    async def write(self, data: bytes) -> None:
        def _w() -> None:
            self._proc.stdin.write(data)  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]

        await asyncio.get_running_loop().run_in_executor(None, _w)

    def resize(self, rows: int, cols: int) -> None:
        return None

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._proc.kill()


class SessionApi:
    """Machine-signed calls to the control plane (all outbound HTTPS)."""

    def __init__(self, base_url: str, machine_id: str, machine_secret: str, *, transport: Any = None, timeout: float = 30.0) -> None:
        self.base_url, self.machine_id, self.machine_secret = base_url.rstrip("/"), machine_id, machine_secret
        self.transport, self.timeout = transport, timeout

    def post(self, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        from .jobrunner import _signed_post

        resp = _signed_post(f"{self.base_url}/v1/agent/sessions{path}", self.machine_id, self.machine_secret, body or {},
                            transport=self.transport, timeout=self.timeout)
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {}

    def relay_url(self, session_id: str, token: str) -> str:
        base = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/v1/sessions/{session_id}/relay/host?token={token}"


def container_name(session_id: str) -> str:
    return f"ccoin-sess-{session_id}"


def build_session_argv(executor: DockerExecutor, claim: dict[str, Any], workdir: str, run: Optional[RentalRun] = None) -> list[str]:
    """Hardened `docker run -d` for a session. Reuses the executor's argv (read-only rootfs, no caps, non-root,
    pids/memory limits, GPU and isolation-tier flags, NO network unless a B5 egress list was declared; the
    container publishes no port). Private tiers are refused."""
    tier = (claim.get("isolation_tier") or "container").lower()
    if tier in ("sealed", "sealed_pro", "confidential"):
        raise SessionAgentError("private tiers are not supported for sessions")
    if claim.get("rental") and run is None:
        raise SessionAgentError("a rental needs its workspace before the container can start")
    limits: dict[str, Any] = {"gpu_count": int(claim.get("gpu_count") or 0), "max_runtime_seconds": int(claim.get("max_duration_seconds") or 3600) + 120}
    if run is not None:
        limits.update({"memory_mb": run.memory_mb, "cpus": str(run.cpus), "pids_limit": RENTAL_PIDS_LIMIT})
    spec = JobSpec(
        job_id=str(claim["id"]), workload_type="session", model="session", input={}, execution_mode="hardware",
        image_digest=claim["image_digest"], isolation_tier=tier, network_policy=claim.get("network_policy"),
        limits=limits,
    )
    prefix, image, suffix = executor.build_argv_parts(spec, workdir)
    if prefix[:2] != ["timeout", "--signal=KILL"] or image != claim["image_digest"] or suffix:
        raise SessionAgentError("unexpected executor argv")
    argv = list(prefix[3:])  # drop the `timeout` wrapper: the container is detached; this agent enforces the deadline
    i = argv.index("--rm") + 1
    # A terminal rental on the customer's own image: keep stdin open with a terminal, so an image whose default command is a shell or a
    # REPL (python, ubuntu, bash) stays up for the terminal to attach to instead of exiting at once. Kit sessions and web-page
    # (tcp) rentals are unchanged.
    keep_open = ["-i", "-t"] if claim.get("rental") and claim.get("mode") != "tcp" else []
    argv[i:i] = ["-d", *keep_open, "--name", container_name(spec.job_id)]
    if any(a in ("-p", "--publish", "-P", "--network=host", "--privileged") for a in argv):
        raise SessionAgentError("session container must not publish ports")
    if run is not None:
        # The customer's workspace: the one folder this container may write besides /out and its tmpfs. Docker bind mounts cannot carry
        # nosuid/nodev; the container runs with every capability dropped and no-new-privileges, so a set-uid file or a device node in it
        # gives nothing.
        argv += [
            "--shm-size", f"{run.shm_mb}m",
            "--mount", f"type=bind,source={run.workspace.path},target=/workspace",
            "--workdir", "/workspace",
            "-e", "HOME=/workspace", "-e", "TMPDIR=/workspace/.tmp",
            "-e", "COMPUTECOIN_WORKSPACE=/workspace", "-e", "COMPUTECOIN_RESULTS=/workspace/results",
        ]
    if claim.get("mode") == "tcp":
        # Kit session (tcp): the kit's own command is the container's long-running main process, and the app learns
        # the browser-side base path (for apps like Jupyter that must be told their URL prefix). Flags go before the
        # image; the command override is `--entrypoint cmd[0] <image> cmd[1:]`.
        argv += ["-e", "COMPUTECOIN_SESSION_BASE_PATH=" + SESSION_BASE_PATH.format(sid=spec.job_id)]
        cmd = claim.get("command")
        if cmd:
            if (not isinstance(cmd, list) or not 1 <= len(cmd) <= MAX_KIT_ARGS
                    or not all(isinstance(c, str) and c and "\x00" not in c for c in cmd)):
                raise SessionAgentError("invalid kit command")
            argv += ["--entrypoint", cmd[0], image, *cmd[1:]]
        else:
            argv.append(image)
    else:
        argv.append(image)
    if run is not None:
        try:
            assert_rental_argv_safe(argv, [workdir, run.workspace.path])
        except RentalError as exc:
            raise SessionAgentError(str(exc)) from exc
    return argv


def build_attach_argv(claim: dict[str, Any]) -> list[str]:
    name = container_name(str(claim["id"]))
    if claim.get("mode") == "tcp":
        port = int(claim.get("tcp_port") or 0)
        if not 1 <= port <= 65535:
            raise SessionAgentError("tcp_port required")
        return ["docker", "exec", "-i", name, "nc", "127.0.0.1", str(port)]
    cmd = claim.get("command") or ["/bin/bash"]
    return ["docker", "exec", "-it", name, *[str(c) for c in cmd]]


class SessionAgent:
    def __init__(
        self,
        api: SessionApi,
        executor: DockerExecutor,
        *,
        runner: Callable[[list[str]], Any] = lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=120),
        ws_connect: Optional[Callable[[str], Any]] = None,
        endpoint_factory: Optional[Callable[[dict[str, Any]], Endpoint]] = None,
        workdir: str = "/tmp",
        clock: Callable[[], float] = time.monotonic,
        http_opener: Optional[Callable[[list[str]], Any]] = None,
        rental: Optional[RentalSettings] = None,
        file_transport: Any = None,
        monitor_seconds: float = MONITOR_SECONDS,
        autosave_seconds: float = AUTOSAVE_SECONDS,
        thermal_guard: Any = None,
        claim_gate: Optional[Callable[[], bool]] = None,
        upload: Optional[Callable[..., dict[str, Any]]] = None,
    ) -> None:
        self.api, self.executor, self._run, self._clock = api, executor, runner, clock
        self._ws_connect = ws_connect
        self._endpoint_factory = endpoint_factory or self._default_endpoint
        self.workdir = workdir
        self._dirs: dict[str, str] = {}
        self._http_opener = http_opener or open_docker_stream
        # container rental (design note section 8); a host that did not opt in has rental.allowed False and never starts one
        self.rental = rental or RentalSettings(allowed=False, root=os.path.join(workdir, "rental"))
        self._file_transport = file_transport
        self._monitor_seconds = monitor_seconds
        self._autosave_seconds = autosave_seconds
        self.thermal_guard = thermal_guard
        self.claim_gate = claim_gate
        self._upload = upload
        self._runs: dict[str, RentalRun] = {}
        self._aborts: dict[str, str] = {}
        self._export_active: set[str] = set()
        self._interrupt_events: dict[str, Any] = {}
        self._bg: set[Any] = set()  # keeps background export tasks alive until they finish

    @staticmethod
    def _default_endpoint(claim: dict[str, Any]) -> Endpoint:
        argv = build_attach_argv(claim)
        return TcpBridgeEndpoint(argv) if claim.get("mode") == "tcp" else PtyEndpoint(argv)

    def _connect(self, url: str) -> Any:
        if self._ws_connect is not None:
            return self._ws_connect(url)
        import websockets  # imported lazily: only session hosts need it

        return websockets.connect(url, max_size=512 * 1024, ping_interval=20)

    def claim(self, static_key: X25519PrivateKey) -> Optional[dict[str, Any]]:
        status, data = self.api.post("/claim", {"host_pubkey": _pub_hex(static_key)})
        if status != 200:
            raise SessionAgentError(f"claim failed: HTTP {status}")
        return data.get("session")

    def _session_dir(self, claim: dict[str, Any]) -> str:
        """A private, empty, per-session dir for the /out bind. NEVER the shared work dir itself (the default is the
        host's /tmp: binding that would hand the container every other job's inputs and outputs)."""
        sid = str(claim["id"])
        path = self._dirs.get(sid)
        if path is None:
            os.makedirs(self.workdir, exist_ok=True)
            path = tempfile.mkdtemp(prefix="ccoin-sess-", dir=self.workdir)
            get_platform().paths.set_mode(path, 0o777)  # the container runs as nobody
            self._dirs[sid] = path
        return path

    def start_container(self, claim: dict[str, Any]) -> None:
        argv = build_session_argv(self.executor, claim, self._session_dir(claim), self._runs.get(str(claim["id"])))
        if claim.get("network_policy") and (claim["network_policy"] or {}).get("egress"):
            spec = JobSpec(job_id=str(claim["id"]), workload_type="session", model="session", input={}, network_policy=claim["network_policy"])
            self.executor._setup_egress(spec, self.executor._egress_list(spec), self.workdir)
        res = self._run(argv)
        if getattr(res, "returncode", 1) != 0:
            raise SessionAgentError(f"docker run failed: {(getattr(res, 'stderr', '') or '')[:200]}")

    def _unlock_workspace(self, sid: str, *, results_only: bool = False) -> None:
        """Make everything the container created readable and removable by this agent. The container runs as another user, so a file it
        saved as 0600 (or a folder it made 0700) would otherwise be unreadable to the export and impossible to delete. The container
        user owns those files, so it can change them; this is best effort (an image without ``chmod`` just keeps the old behaviour)."""
        with contextlib.suppress(Exception):
            self._run(["docker", "exec", container_name(sid), "chmod", "-R", "a+rwX", "/workspace/results" if results_only else "/workspace"])

    def stop_container(self, claim: dict[str, Any]) -> None:
        sid = str(claim["id"])
        if sid in self._runs:
            self._unlock_workspace(sid)  # while the container still exists
        with contextlib.suppress(Exception):
            self._run(["docker", "rm", "-f", container_name(str(claim["id"]))])
        session_dir = self._dirs.pop(str(claim["id"]), None)
        if session_dir:
            shutil.rmtree(session_dir, ignore_errors=True)
        if claim.get("network_policy") and (claim["network_policy"] or {}).get("egress"):
            spec = JobSpec(job_id=str(claim["id"]), workload_type="session", model="session", input={})
            with contextlib.suppress(Exception):
                self.executor._teardown_egress(spec, self.workdir)

    # ---- container rental (design note section 8) -------------------------------------------------------------------
    def prepare_rental(self, claim: dict[str, Any]) -> None:
        """The workspace and the staged input files, BEFORE the container starts. Raises SessionAgentError (nothing is billed then)."""
        sid = str(claim["id"])
        if not self.rental.allowed:
            raise SessionAgentError("container rental is not turned on for this computer")
        ws: Optional[Workspace] = None
        try:
            memory_mb, cpus, shm_mb, quota = resolve_limits(self.rental, claim)
            inputs = self._validated_inputs(claim, quota)
            ws = Workspace(self.rental.root, sid, quota)
            ws.create(need_free_bytes=sum(i["size_bytes"] for i in inputs))
            self._runs[sid] = RentalRun(workspace=ws, memory_mb=memory_mb, cpus=cpus, shm_mb=shm_mb, quota_bytes=quota)
            self._stage_inputs(sid, ws, inputs)
        except RentalError as exc:
            if sid not in self._runs and ws is not None:
                ws.delete()  # created but never registered: nothing else will clean it
            raise SessionAgentError(str(exc)) from exc

    @staticmethod
    def _validated_inputs(claim: dict[str, Any], quota_bytes: int) -> list[dict[str, Any]]:
        raw = claim.get("inputs") or []
        if not isinstance(raw, list) or len(raw) > rental_mod.MAX_INPUTS:
            raise RentalError("too many input files")
        out: list[dict[str, Any]] = []
        paths: set[str] = set()
        dirs: set[str] = set()
        total = 0
        for item in raw:
            if not isinstance(item, dict):
                raise RentalError("an input file entry is not valid")
            path = clean_relpath(item.get("path"), allow_results_prefix=False)
            size, sha = item.get("size_bytes"), str(item.get("sha256") or "").lower()
            if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not rental_mod._SHA_RE.match(sha):
                raise RentalError("an input file entry has no valid size or checksum")
            if path in paths:
                raise RentalError("two input files have the same path")
            paths.add(path)
            parts = path.split("/")
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
            total += size
            out.append({"path": path, "size_bytes": size, "sha256": sha})
        if paths & dirs:
            raise RentalError("an input file path is also used as a folder")
        if total > quota_bytes:
            raise RentalError("the input files are larger than the workspace")
        return out

    def _stage_inputs(self, sid: str, ws: Workspace, inputs: list[dict[str, Any]]) -> None:
        if not inputs:
            return
        from .runtime_v2 import V2Error, download_verified

        links: dict[str, str] = {}
        fetched_at = 0.0

        def refresh() -> None:
            nonlocal links, fetched_at
            status, data = self.api.post(f"/{sid}/inputs")
            items = data.get("inputs") if status == 200 and isinstance(data, dict) else None
            if not isinstance(items, list):
                raise RentalError(f"the input files could not be fetched (HTTP {status})")
            by_path = {it["path"]: it for it in items if isinstance(it, dict) and isinstance(it.get("path"), str)}
            wanted = {w["path"]: w for w in inputs}
            if set(by_path) != set(wanted):
                raise RentalError("the control plane listed different input files than were claimed")
            fresh: dict[str, str] = {}
            for path, want in wanted.items():
                got = by_path[path]
                url = got.get("url")
                if got.get("size_bytes") != want["size_bytes"] or str(got.get("sha256") or "").lower() != want["sha256"]:
                    raise RentalError("an input file's size or checksum changed")
                if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
                    raise RentalError("an input file has no usable download link")
                fresh[path] = url
            links, fetched_at = fresh, self._clock()

        for want in inputs:
            if not links or self._clock() - fetched_at > INPUT_URL_REFRESH_SECONDS:
                refresh()
            dest = resolve_inside(ws.path, want["path"])
            self._make_parents(ws.path, want["path"])
            try:
                download_verified(links[want["path"]], dest, sha256=want["sha256"], size=want["size_bytes"], transport=self._file_transport)
            except V2Error as exc:
                raise RentalError(f"an input file could not be downloaded ({exc.code})") from exc
            get_platform().paths.set_mode(dest, 0o666)  # the container user is not this user

    @staticmethod
    def _make_parents(root: str, rel: str) -> None:
        cur = root
        for part in rel.split("/")[:-1]:
            cur = os.path.join(cur, part)
            if os.path.lexists(cur):
                if os.path.islink(cur) or not os.path.isdir(cur):
                    raise RentalError("an input file path runs into something that is not a folder")
                continue
            os.mkdir(cur)
            get_platform().paths.set_mode(cur, 0o777)

    def _check_rental(self, claim: dict[str, Any]) -> Optional[str]:
        """Why a running rental must stop now, or None. Order matters: a miner first, then the host's own needs, then the disk."""
        sid = str(claim["id"])
        run = self._runs.get(sid)
        if run is None:
            return None
        res = self._run(["docker", "top", container_name(sid), "-eo", "comm,args"])
        if getattr(res, "returncode", 1) == 0 and detect_miner(getattr(res, "stdout", "") or ""):
            return "miner_detected"
        event = self._interrupt_events.get(sid)
        if event is not None and event.is_set():
            return "interrupted"
        guard = self.thermal_guard
        if guard is not None:
            from .thermal import HARD_STOP, SENSOR_LOST

            with contextlib.suppress(Exception):
                if guard.check() in (HARD_STOP, SENSOR_LOST):
                    return "thermal"
        if run.workspace.over_quota() or run.workspace.disk_low():
            return "workspace_full"
        return None

    async def _monitor(self, claim: dict[str, Any]) -> None:
        sid = str(claim["id"])
        last_save = self._clock()
        while sid not in self._aborts:
            await asyncio.sleep(self._monitor_seconds)
            if self._autosave_seconds > 0 and self._clock() - last_save >= self._autosave_seconds:
                last_save = self._clock()
                self._kick_export(claim, autosave=True)  # changed results only; nothing is sent when nothing changed
            try:
                reason = await asyncio.to_thread(self._check_rental, claim)
            except Exception as exc:  # noqa: BLE001 - a failing check must not end the rental by itself
                logging.getLogger(__name__).warning("rental check failed: %s", type(exc).__name__)
                continue
            if reason:
                self._aborts[sid] = reason
                with contextlib.suppress(Exception):  # stop the work at once; the end-of-session steps follow
                    await asyncio.to_thread(self._unlock_workspace, sid)
                    await asyncio.to_thread(self._run, ["docker", "rm", "-f", container_name(sid)])
                return

    def export_results(self, claim: dict[str, Any], *, autosave: bool = False) -> Any:
        sid = str(claim["id"])
        run = self._runs.get(sid)
        if run is None:
            return None
        self._unlock_workspace(sid, results_only=True)  # a no-op once the container is gone; while it runs it makes results readable (only
        # that folder: a whole-workspace chmod every few minutes would crawl through a large dataset)
        return _export_results(self.api, sid, run, upload=self._upload, transport=self._file_transport, quiet_if_unchanged=autosave)

    def _kick_export(self, claim: dict[str, Any], *, autosave: bool = False) -> None:
        """The customer pressed "save results now": run one export in the background (never two at once)."""
        sid = str(claim["id"])
        if sid in self._export_active:
            return
        self._export_active.add(sid)

        async def go() -> None:
            try:
                await asyncio.to_thread(self.export_results, claim, autosave=autosave)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("export failed: %s", type(exc).__name__)
            finally:
                self._export_active.discard(sid)

        task = asyncio.get_running_loop().create_task(go())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def finish_rental(self, claim: dict[str, Any], export: bool) -> None:
        """After the container is gone: save the results (when asked to), then delete the workspace. Always deletes."""
        sid = str(claim["id"])
        run = self._runs.get(sid)
        if run is None:
            return
        try:
            if export:
                self.export_results(claim)
        except Exception as exc:  # noqa: BLE001 - an export problem never blocks the cleanup
            logging.getLogger(__name__).warning("final export failed: %s", type(exc).__name__)
        finally:
            with run.lock:  # an export still running in the background finishes first
                run.workspace.delete()
                if os.path.exists(run.workspace.top):
                    self._delete_with_helper(claim, run)
            self._runs.pop(sid, None)
            self._aborts.pop(sid, None)
            self._interrupt_events.pop(sid, None)

    def _delete_with_helper(self, claim: dict[str, Any], run: RentalRun) -> None:
        """The workspace could not be deleted (files left by the container that this user cannot change). Open them up with a throw-away
        container of the same image, running as the container's own user with no network and no capabilities, then delete again."""
        argv = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", self.executor.NONROOT_USER, "--mount", f"type=bind,source={run.workspace.path},target=/w",
                "--entrypoint", "chmod", str(claim["image_digest"]), "-R", "a+rwX", "/w"]
        with contextlib.suppress(Exception):
            self._run(argv)
        run.workspace.delete()
        if os.path.exists(run.workspace.top):
            logging.getLogger(__name__).warning("a rental workspace could not be fully deleted: %s", run.workspace.top)

    def cleanup_stale(self) -> int:
        """At start: delete rental folders whose container no longer exists (a crashed agent leaves them). Folders of live rentals, and
        anything when Docker cannot be asked, are left alone."""
        if not os.path.isdir(self.rental.root):
            return 0
        res = self._run(["docker", "ps", "-a", "--format", "{{.Names}}"])
        if getattr(res, "returncode", 1) != 0:
            return 0
        live = set((getattr(res, "stdout", "") or "").split())
        n = 0
        for path in list_stale(self.rental.root, live):
            if os.path.basename(path) in self._runs:
                continue
            shutil.rmtree(path, ignore_errors=True)
            n += 1
        return n

    async def serve(self, claim: dict[str, Any], static_key: X25519PrivateKey) -> str:
        """Run one claimed session to its end. Returns the end reason. Always removes the container."""
        sid = str(claim["id"])
        deadline = self._clock() + int(claim.get("max_duration_seconds") or 3600)
        failed = False
        started = False
        rental = bool(claim.get("rental"))
        monitor: Optional[asyncio.Task] = None
        try:
            if rental:
                self._interrupt_events[sid] = interrupts.REGISTRY.register(sid)  # `computecoin-agent pause` stops rentals too
                await asyncio.to_thread(self.prepare_rental, claim)
            await asyncio.to_thread(self.start_container, claim)
            started = True
            await asyncio.to_thread(self.api.post, f"/{sid}/started")
            if rental:
                monitor = asyncio.create_task(self._monitor(claim))
            token = claim["host_token"]
            reason = "ended"
            while self._clock() < deadline:
                if self._aborts.get(sid):
                    reason = "stop"
                    break
                try:
                    reason = await self._serve_socket(claim, static_key, token, deadline)
                except (OSError, SessionAgentError, asyncio.TimeoutError, *_relay_errors()):
                    # A dropped relay socket (websockets raises ConnectionClosedError, which is not an OSError) is a
                    # reconnect with a fresh token, not the end of the session.
                    reason = "reconnect"
                if reason in ("stop", "ended") or self._aborts.get(sid):
                    reason = "stop" if self._aborts.get(sid) else reason
                    break
                st, data = await asyncio.to_thread(self.api.post, f"/{sid}/token")  # fresh single-use token
                if st != 200:
                    break
                token = data["host_token"]
                await asyncio.sleep(2)
            return reason
        except Exception:
            failed = True
            raise
        finally:
            if monitor is not None:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await monitor
            abort = self._aborts.get(sid)
            try:
                await asyncio.to_thread(self.stop_container, claim)
            finally:
                try:
                    await asyncio.to_thread(self.api.post, f"/{sid}/ended", {"failed": bool(failed or abort == "miner_detected")})
                finally:
                    if rental:
                        interrupts.REGISTRY.unregister(sid)
                        # Saved AFTER the session is ended, so the time spent saving is never billed (the upload routes accept it
                        # for the export window). Never after a miner was found, and never when nothing ever started.
                        await asyncio.to_thread(self.finish_rental, claim, bool(started and abort != "miner_detected"))

    async def _serve_socket(self, claim: dict[str, Any], static_key: X25519PrivateKey, token: str, deadline: float) -> str:
        sid = str(claim["id"])
        async with self._connect(self.api.relay_url(sid, token)) as ws:
            crypto: Optional[SessionCrypto] = None
            endpoint: Optional[Endpoint] = None
            stop = asyncio.Event()
            pump_task: Optional[asyncio.Task] = None
            bridge: Optional[HttpBridge] = None
            send_lock = asyncio.Lock()  # seal + send must be atomic so counters match the wire order

            async def send_sealed(cr: SessionCrypto, kind: int, payload: bytes) -> None:
                async with send_lock:
                    await ws.send(cr.seal(kind, payload))

            async def heartbeat() -> None:
                failures = 0
                while not stop.is_set():
                    if self._aborts.get(sid):  # a rental the host's own checks stopped (disk full, too hot, owner needs it, miner)
                        stop.set()
                        with contextlib.suppress(Exception):
                            await ws.close()
                        return
                    try:
                        st, data = await asyncio.to_thread(self.api.post, f"/{sid}/heartbeat")
                    except Exception:
                        # A network blip must not silently end the heartbeats (the control plane would then reap a live
                        # session): retry a few times, then treat the session as lost.
                        failures += 1
                        if failures < HEARTBEAT_MAX_FAILURES and self._clock() < deadline:
                            await asyncio.sleep(HEARTBEAT_RETRY_SECONDS)
                            continue
                        st, data = 0, {}
                    else:
                        failures = 0
                    if st == 200 and data.get("export") and claim.get("rental"):
                        self._kick_export(claim)  # the customer pressed "save results now"
                    if st != 200 or data.get("stop") or self._clock() >= deadline:
                        stop.set()
                        with contextlib.suppress(Exception):
                            await ws.close()
                        return
                    await asyncio.sleep(HEARTBEAT_SECONDS)

            async def out_pump(ep: Endpoint, cr: SessionCrypto) -> None:
                while True:
                    data = await ep.read()
                    if not data:
                        await send_sealed(cr, T_CLOSE, b"")  # shell exited; the session lives until stopped
                        return
                    for i in range(0, len(data), MAX_PLAINTEXT):
                        await send_sealed(cr, T_DATA, data[i : i + MAX_PLAINTEXT])

            hb = asyncio.create_task(heartbeat())
            try:
                async for msg in ws:
                    if isinstance(msg, str):
                        try:
                            hello = json.loads(msg)
                        except ValueError:
                            continue
                        if not isinstance(hello, dict) or hello.get("type") != "hello":
                            continue
                        if pump_task:
                            pump_task.cancel()
                        if endpoint:
                            await endpoint.close()
                        if bridge:
                            await bridge.close()
                            bridge = None
                        crypto = SessionCrypto(sid, static_key, str(hello.get("epk", "")))
                        await ws.send(json.dumps({"type": "hello_ack", "epk": crypto.ephemeral_pub_hex}))
                        endpoint = self._endpoint_factory(claim)
                        await send_sealed(crypto, T_READY, b"")
                        if claim.get("mode") == "tcp" and claim.get("tcp_port"):
                            def _mk(cr: SessionCrypto = crypto):
                                async def _send(p: bytes) -> None:
                                    await send_sealed(cr, T_HTTP, p)
                                return _send
                            bridge = HttpBridge(_mk(), build_attach_argv(claim), int(claim["tcp_port"]), opener=self._http_opener)
                        pump_task = asyncio.create_task(out_pump(endpoint, crypto))
                        continue
                    if crypto is None or endpoint is None:
                        continue  # ciphertext before a handshake: ignore
                    kind, payload = crypto.open(bytes(msg))  # raises on tamper: the socket is dropped
                    if kind == T_DATA:
                        await endpoint.write(payload)
                    elif kind == T_RESIZE:
                        with contextlib.suppress(Exception):
                            r = json.loads(payload)
                            endpoint.resize(int(r["rows"]), int(r["cols"]))
                    elif kind == T_HTTP:
                        if bridge is not None:
                            await bridge.on_frame(payload)
                    elif kind == T_CLOSE:
                        break  # the viewer left; the session keeps running (idle timeout or an explicit stop ends it)
            finally:
                hb.cancel()
                if bridge:
                    await bridge.close()
                if pump_task:
                    pump_task.cancel()
                if endpoint:
                    await endpoint.close()
            return "stop" if stop.is_set() else "reconnect"


def new_static_key() -> X25519PrivateKey:
    return X25519PrivateKey.generate()


def run_forever(api: SessionApi, executor: DockerExecutor, *, poll_seconds: float = 5.0, once: bool = False,
                sleeper: Callable[[float], None] = time.sleep, agent: Optional[SessionAgent] = None) -> None:
    agent = agent or SessionAgent(api, executor)
    try:
        agent.cleanup_stale()  # rental folders a crashed agent left behind
    except Exception as exc:  # noqa: BLE001 - housekeeping must never stop the loop
        logging.getLogger(__name__).warning("rental cleanup: %s", type(exc).__name__)
    while True:
        gate = agent.claim_gate
        if gate is not None:
            try:
                open_for_work = bool(gate())  # the computer is too hot, or its owner needs it: no new sessions now
            except Exception:  # noqa: BLE001 - an unreadable gate means "closed"
                open_for_work = False
            if not open_for_work:
                if once:
                    return
                sleeper(poll_seconds)
                continue
        key = new_static_key()
        try:
            claim = agent.claim(key)
            if claim:
                asyncio.run(agent.serve(claim, key))
        except Exception as exc:
            # One network blip on the claim, or one failed session, must not end the host's session loop.
            if once:
                raise
            logging.getLogger(__name__).warning("session loop: %s", type(exc).__name__)
        if once:
            return
        sleeper(poll_seconds)


def start_background_sessions(
    config: Any,
    *,
    thermal_guard: Any = None,
    owner: Any = None,
    engine_available: Optional[Callable[[], bool]] = None,
    api: Optional[SessionApi] = None,
    executor: Optional[DockerExecutor] = None,
    run_loop: Optional[Callable[..., None]] = None,
) -> Optional["threading.Thread"]:
    """Serve interactive sessions from inside ``computecoin-agent start``, in a background thread. Returns the thread, or None when sessions
    are not served here (turned off in the settings, a Windows host, or no usable container engine).

    The thread never raises: a crash in it is logged and the job worker carries on. Rentals still need the host's own opt-in
    (``allow_container_rental``); Kit sessions do not."""
    import threading

    from .platform import PLATFORM_WINDOWS

    if getattr(config, "serve_sessions", True) is not True:
        return None
    plat = get_platform()
    if plat.name == PLATFORM_WINDOWS:
        return None
    try:
        usable = bool(engine_available() if engine_available is not None else plat.runtime.engine_available())
    except Exception:  # noqa: BLE001
        usable = False
    if not usable:
        return None
    api = api or SessionApi(config.api_url, config.machine_id, config.machine_secret)
    executor = executor or DockerExecutor()

    def gate() -> bool:
        if thermal_guard is not None and not thermal_guard.may_claim():
            return False
        if owner is not None and owner.availability() != "online":
            return False
        return True

    # The per-session scratch folder follows the host's `work_dir` setting (default /tmp). A container engine that runs in a virtual
    # machine (Docker Desktop) cannot see /tmp, so its host points `work_dir` at a folder the engine can share.
    work_dir = getattr(config, "work_dir", None)
    agent = SessionAgent(api, executor, rental=RentalSettings.from_config(config), thermal_guard=thermal_guard, claim_gate=gate,
                         workdir=work_dir if isinstance(work_dir, str) and work_dir else "/tmp")
    loop = run_loop or run_forever

    def target() -> None:
        try:
            loop(api, executor, agent=agent)
        except BaseException as exc:  # noqa: BLE001 - the session thread must never take `start` down
            logging.getLogger(__name__).error("the session thread stopped: %s", type(exc).__name__)

    thread = threading.Thread(target=target, name="computecoin-sessions", daemon=True)
    thread.start()
    return thread
