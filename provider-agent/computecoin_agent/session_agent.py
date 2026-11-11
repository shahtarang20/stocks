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

from .platform import get_platform
from .executor import DockerExecutor, ExecutorError, JobSpec
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


def build_session_argv(executor: DockerExecutor, claim: dict[str, Any], workdir: str) -> list[str]:
    """Hardened `docker run -d` for a session. Reuses the executor's argv (read-only rootfs, no caps, non-root,
    pids/memory limits, GPU and isolation-tier flags, NO network unless a B5 egress list was declared; the
    container publishes no port). Private tiers are refused."""
    tier = (claim.get("isolation_tier") or "container").lower()
    if tier in ("sealed", "sealed_pro", "confidential"):
        raise SessionAgentError("private tiers are not supported for sessions")
    spec = JobSpec(
        job_id=str(claim["id"]), workload_type="session", model="session", input={}, execution_mode="hardware",
        image_digest=claim["image_digest"], isolation_tier=tier, network_policy=claim.get("network_policy"),
        limits={"gpu_count": int(claim.get("gpu_count") or 0), "max_runtime_seconds": int(claim.get("max_duration_seconds") or 3600) + 120},
    )
    prefix, image, suffix = executor.build_argv_parts(spec, workdir)
    if prefix[:2] != ["timeout", "--signal=KILL"] or image != claim["image_digest"] or suffix:
        raise SessionAgentError("unexpected executor argv")
    argv = list(prefix[3:])  # drop the `timeout` wrapper: the container is detached; this agent enforces the deadline
    i = argv.index("--rm") + 1
    argv[i:i] = ["-d", "--name", container_name(spec.job_id)]
    if any(a in ("-p", "--publish", "-P", "--network=host", "--privileged") for a in argv):
        raise SessionAgentError("session container must not publish ports")
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
    ) -> None:
        self.api, self.executor, self._run, self._clock = api, executor, runner, clock
        self._ws_connect = ws_connect
        self._endpoint_factory = endpoint_factory or self._default_endpoint
        self.workdir = workdir
        self._dirs: dict[str, str] = {}
        self._http_opener = http_opener or open_docker_stream

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
        argv = build_session_argv(self.executor, claim, self._session_dir(claim))
        if claim.get("network_policy") and (claim["network_policy"] or {}).get("egress"):
            spec = JobSpec(job_id=str(claim["id"]), workload_type="session", model="session", input={}, network_policy=claim["network_policy"])
            self.executor._setup_egress(spec, self.executor._egress_list(spec), self.workdir)
        res = self._run(argv)
        if getattr(res, "returncode", 1) != 0:
            raise SessionAgentError(f"docker run failed: {(getattr(res, 'stderr', '') or '')[:200]}")

    def stop_container(self, claim: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._run(["docker", "rm", "-f", container_name(str(claim["id"]))])
        session_dir = self._dirs.pop(str(claim["id"]), None)
        if session_dir:
            shutil.rmtree(session_dir, ignore_errors=True)
        if claim.get("network_policy") and (claim["network_policy"] or {}).get("egress"):
            spec = JobSpec(job_id=str(claim["id"]), workload_type="session", model="session", input={})
            with contextlib.suppress(Exception):
                self.executor._teardown_egress(spec, self.workdir)

    async def serve(self, claim: dict[str, Any], static_key: X25519PrivateKey) -> str:
        """Run one claimed session to its end. Returns the end reason. Always removes the container."""
        sid = str(claim["id"])
        deadline = self._clock() + int(claim.get("max_duration_seconds") or 3600)
        failed = False
        try:
            await asyncio.to_thread(self.start_container, claim)
            await asyncio.to_thread(self.api.post, f"/{sid}/started")
            token = claim["host_token"]
            reason = "ended"
            while self._clock() < deadline:
                try:
                    reason = await self._serve_socket(claim, static_key, token, deadline)
                except (OSError, SessionAgentError, asyncio.TimeoutError, *_relay_errors()):
                    # A dropped relay socket (websockets raises ConnectionClosedError, which is not an OSError) is a
                    # reconnect with a fresh token, not the end of the session.
                    reason = "reconnect"
                if reason in ("stop", "ended"):
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
            await asyncio.to_thread(self.stop_container, claim)
            await asyncio.to_thread(self.api.post, f"/{sid}/ended", {"failed": failed})

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
                sleeper: Callable[[float], None] = time.sleep) -> None:  # pragma: no cover
    agent = SessionAgent(api, executor)
    while True:
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
