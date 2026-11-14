"""Supervisor <-> runner control channel (cross-platform plan W4, section 3.3).

Pure Python, no OS-specific calls, so the same classes run on Linux (tests, a Linux runner started with
``--control-socket``) and on Windows (the supervisor is the CLIENT, the runner inside the WSL2 distro is the SERVER).

Protocol summary (full text: docs/architecture/windows-control-channel.md)
--------------------------------------------------------------------------
* Transport: one TCP connection to a literal loopback address (127.0.0.0/8, ::1) or, only when the operator passes
  ``allow_private=True``, a literal private address (the WSL2 distro's address). Wildcard addresses ("0.0.0.0", "::")
  and host names are refused: there is no LAN listener.
* Authentication: a per-install random token that NEVER crosses the wire. HMAC-SHA256 challenge/response with a
  nonce from each side (mutual), compared in constant time. A wrong token closes the connection with no detail.
* After the handshake every frame is ``len(4, big endian) || json || mac(32)``, the MAC covering the direction and a
  per-direction counter, so frames cannot be replayed, reordered or injected by a process that only sees the stream.
* Caps: handshake frames <= 512 bytes, message frames <= 64 KiB (checked BEFORE the body is read), per-connection
  message rate, connection-attempt rate and an auth-failure lockout.
* Messages are versioned (``v``), typed and strictly validated: unknown types, unknown fields, wrong types and
  out-of-range numbers are refused (L1 fail closed).

Nothing here talks to the API or to money code. ``RunnerControl`` is the runner-side state machine used by
``jobrunner.poll_loop`` when (and only when) a runner is started with ``--control-socket``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
MAX_HANDSHAKE_BYTES = 512
MAC_LEN = 32
TOKEN_ENV = "COMPUTECOIN_CONTROL_TOKEN"
MIN_TOKEN_CHARS = 32
HANDSHAKE_TIMEOUT_S = 5.0

AVAILABILITY_STATES = ("online", "throttled", "yielding", "maintenance", "draining")

SUPERVISOR_TO_RUNNER = ("start", "drain", "stop", "set_limits", "set_availability")
RUNNER_TO_SUPERVISOR = ("ready", "job_started", "job_finished", "error", "usage")

_LABEL = b"cc-ctl-v1|"


class ControlChannelError(RuntimeError):
    """Base class for every control channel failure."""


class ProtocolError(ControlChannelError):
    """A frame or message violated the protocol (oversize, malformed, wrong type, bad MAC, replay)."""


class AuthError(ControlChannelError):
    """The peer did not prove knowledge of the token (or we could not prove it to the peer)."""


class RefusedError(ControlChannelError):
    """The server refused to talk (rate cap, lockout, peer address not allowed)."""


# --------------------------------------------------------------------------- #
# Token handling
# --------------------------------------------------------------------------- #
def generate_token() -> str:
    """A fresh per-install token (256 random bits, hex)."""
    return secrets.token_hex(32)


def _check_token(token: Any) -> bytes:
    if not isinstance(token, str) or len(token) < MIN_TOKEN_CHARS:
        raise AuthError(f"control token must be a string of at least {MIN_TOKEN_CHARS} characters")
    return token.encode("utf-8")


def load_token(token_file: Optional[str] = None, env: Optional[dict[str, str]] = None) -> str:
    """Read the token from a file (preferred) or from ``COMPUTECOIN_CONTROL_TOKEN``. Never from argv.

    On POSIX a token file readable by group or other is refused (fail closed). On Windows the file's ACL is the
    supervisor/provisioner's job (windows/paths.py), so the mode check is skipped there.
    """
    env = os.environ if env is None else env
    if token_file:
        try:
            st = os.stat(token_file)
        except OSError as exc:
            raise AuthError(f"cannot read control token file: {exc.strerror or exc}") from exc
        if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AuthError("control token file must not be accessible to group or others (chmod 600)")
        with open(token_file, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    else:
        token = (env.get(TOKEN_ENV) or "").strip()
    _check_token(token)
    return token


# --------------------------------------------------------------------------- #
# Address policy: no LAN listener
# --------------------------------------------------------------------------- #
def validate_address(host: str, *, allow_private: bool = False) -> str:
    """Return the canonical literal address, or raise ``ValueError``.

    Loopback is always accepted. A private/link-local literal (the distro's address) is accepted only with
    ``allow_private``. Wildcards, multicast, public addresses and host names are always refused.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("control address must be a literal IP address, not a host name") from None
    if ip.is_unspecified or ip.is_multicast:
        raise ValueError("control address must not be a wildcard or multicast address")
    if ip.is_loopback:
        return str(ip)
    if allow_private and (ip.is_private or ip.is_link_local) and not ip.is_global:
        return str(ip)
    raise ValueError("control address must be loopback (or a private address with allow_private)")


def parse_hostport(value: str) -> tuple[str, int]:
    """Parse ``HOST:PORT`` (``[::1]:PORT`` for IPv6)."""
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port_s = rest.lstrip(":")
    else:
        host, _, port_s = value.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError("control socket must look like HOST:PORT") from None
    if not host or not 0 <= port <= 65535:
        raise ValueError("control socket must look like HOST:PORT")
    return host, port


def _peer_allowed(peer_ip: str, allow_private: bool) -> bool:
    try:
        ip = ipaddress.ip_address(peer_ip.split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return bool(allow_private and (ip.is_private or ip.is_link_local) and not ip.is_global)


# --------------------------------------------------------------------------- #
# Message schema (strict, fail closed)
# --------------------------------------------------------------------------- #
def _is_int(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _is_num(v: Any, lo: float, hi: float) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= float(v) <= hi and float(v) == float(v)


def _is_str(v: Any, max_len: int, min_len: int = 0) -> bool:
    return isinstance(v, str) and min_len <= len(v) <= max_len


# field name -> predicate. Required fields are listed per type.
_FIELDS: dict[str, Callable[[Any], bool]] = {
    "reason": lambda v: _is_str(v, 200),
    "grace_seconds": lambda v: _is_num(v, 0, 3600),
    "max_concurrent_jobs": lambda v: _is_int(v, 0, 64),
    "gpu_temp_cap_c": lambda v: _is_num(v, 0, 150),
    "gpu_temp_resume_c": lambda v: _is_num(v, 0, 150),
    "gpu_temp_hard_stop_c": lambda v: _is_num(v, 0, 150),
    "gpu_power_limit_w": lambda v: v is None or _is_num(v, 1, 2000),
    "availability": lambda v: v in AVAILABILITY_STATES,
    "gpu_temp_c": lambda v: _is_num(v, -50, 200),
    "agent_version": lambda v: _is_str(v, 64),
    "protocol": lambda v: _is_int(v, 1, 1000),
    "state": lambda v: _is_str(v, 32),
    "job_id": lambda v: _is_str(v, 128, 1),
    "status": lambda v: _is_str(v, 32, 1),
    "active_jobs": lambda v: _is_int(v, 0, 1_000_000),
    "draining": lambda v: isinstance(v, bool),
    "started": lambda v: isinstance(v, bool),
    "stopped": lambda v: isinstance(v, bool),
    "code": lambda v: _is_str(v, 64, 1),
    "message": lambda v: _is_str(v, 300),
    "ref_seq": lambda v: _is_int(v, 0, 2**62),
    "jobs_completed": lambda v: _is_int(v, 0, 2**62),
    "jobs_failed": lambda v: _is_int(v, 0, 2**62),
    "uptime_s": lambda v: _is_num(v, 0, 1e12),
    "max_concurrent": lambda v: _is_int(v, 0, 64),
}

# type -> (required, optional)
_SCHEMAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # supervisor -> runner
    "start": ((), ("max_concurrent_jobs", "availability")),
    "drain": ((), ("reason",)),
    "stop": ((), ("reason", "grace_seconds")),
    # The host's owner needs the computer back: stop the running job(s) cleanly and report them host_interrupted.
    "interrupt": ((), ("reason",)),
    "set_limits": (
        (),
        ("max_concurrent_jobs", "gpu_temp_cap_c", "gpu_temp_resume_c", "gpu_temp_hard_stop_c", "gpu_power_limit_w"),
    ),
    "set_availability": (("availability",), ("gpu_temp_c",)),
    # runner -> supervisor
    "ready": (("agent_version", "protocol", "state"), ()),
    "job_started": (("job_id", "active_jobs"), ()),
    "job_finished": (("job_id", "status", "active_jobs", "draining"), ()),
    "error": (("code",), ("message", "ref_seq")),
    "usage": (
        ("active_jobs", "draining", "availability", "started", "stopped"),
        ("jobs_completed", "jobs_failed", "uptime_s", "max_concurrent"),
    ),
}


def validate_data(msg_type: str, data: Any) -> dict[str, Any]:
    """Validate ``data`` for ``msg_type``; return a shallow copy. Raises :class:`ProtocolError`."""
    if msg_type not in _SCHEMAS:
        raise ProtocolError("unknown message type")
    if not isinstance(data, dict):
        raise ProtocolError("message data must be an object")
    required, optional = _SCHEMAS[msg_type]
    allowed = set(required) | set(optional)
    for key, value in data.items():
        if key not in allowed:
            raise ProtocolError(f"unexpected field {key!r} for {msg_type}")
        if not _FIELDS[key](value):
            raise ProtocolError(f"invalid value for {key!r} in {msg_type}")
    for key in required:
        if key not in data:
            raise ProtocolError(f"missing field {key!r} in {msg_type}")
    if msg_type == "set_limits" and not data:
        raise ProtocolError("set_limits needs at least one limit")
    return dict(data)


@dataclass(frozen=True)
class ControlMessage:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    v: int = PROTOCOL_VERSION


def encode_message(msg_type: str, data: Optional[dict[str, Any]], seq: int, *, direction: str) -> bytes:
    """Serialise one message. ``direction`` is ``"to_runner"`` or ``"to_supervisor"`` and fixes the allowed types."""
    allowed = SUPERVISOR_TO_RUNNER if direction == "to_runner" else RUNNER_TO_SUPERVISOR
    if msg_type not in allowed:
        raise ProtocolError(f"{msg_type!r} cannot be sent {direction}")
    clean = validate_data(msg_type, data or {})
    raw = json.dumps({"v": PROTOCOL_VERSION, "seq": seq, "type": msg_type, "data": clean},
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("message too large")
    return raw


def decode_message(raw: bytes, *, direction: str) -> ControlMessage:
    """Parse and validate a received message. ``direction`` is the direction it travelled."""
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("message too large")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("malformed message") from None
    if not isinstance(obj, dict) or set(obj) != {"v", "seq", "type", "data"}:
        raise ProtocolError("malformed envelope")
    if obj["v"] != PROTOCOL_VERSION or isinstance(obj["v"], bool):
        raise ProtocolError("unsupported protocol version")
    if not _is_int(obj["seq"], 0, 2**62):
        raise ProtocolError("bad sequence number")
    allowed = SUPERVISOR_TO_RUNNER if direction == "to_runner" else RUNNER_TO_SUPERVISOR
    if obj["type"] not in allowed:
        raise ProtocolError("message type not allowed in this direction")
    return ControlMessage(type=obj["type"], data=validate_data(obj["type"], obj["data"]), seq=obj["seq"])


# --------------------------------------------------------------------------- #
# Framing + handshake
# --------------------------------------------------------------------------- #
def _recv_exact(sock: Any, n: int, *, idle_ok: bool = False, stall_s: float = 5.0) -> bytes:
    """Read exactly ``n`` bytes. A socket timeout before the FIRST byte is re-raised when ``idle_ok`` (the caller
    polls); once a frame has started, a peer that stalls longer than ``stall_s`` is a protocol error, so a timeout
    can never leave the stream half-read."""
    buf = bytearray()
    deadline: Optional[float] = None
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            if idle_ok and not buf:
                raise
            if deadline is None:
                deadline = time.monotonic() + stall_s
            if time.monotonic() > deadline:
                raise ProtocolError("peer stalled mid-frame") from None
            continue
        if not chunk:
            raise ConnectionError("connection closed")
        buf.extend(chunk)
    return bytes(buf)


def _read_plain(sock: Any, max_len: int) -> bytes:
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    if length == 0 or length > max_len:
        raise ProtocolError("frame size out of range")  # refused before the body is read
    return _recv_exact(sock, length)


def _write_plain(sock: Any, payload: bytes) -> None:
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _mac(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


class FrameChannel:
    """An authenticated, counter-protected message pipe over a connected socket (both ends use this)."""

    def __init__(self, sock: Any, session_key: bytes, *, is_server: bool) -> None:
        self._sock = sock
        self._key = session_key
        self._send_dir = b"S" if is_server else b"C"
        self._recv_dir = b"C" if is_server else b"S"
        self._send_counter = 0
        self._recv_counter = 0
        self._send_lock = threading.Lock()
        self.direction_out = "to_supervisor" if is_server else "to_runner"
        self.direction_in = "to_runner" if is_server else "to_supervisor"

    def send(self, msg_type: str, data: Optional[dict[str, Any]] = None) -> int:
        with self._send_lock:
            seq = self._send_counter
            payload = encode_message(msg_type, data, seq, direction=self.direction_out)
            tag = _mac(self._key, self._send_dir, struct.pack(">Q", seq), payload)
            _write_plain(self._sock, payload + tag)
            self._send_counter += 1
            return seq

    def recv(self) -> ControlMessage:
        (length,) = struct.unpack(">I", _recv_exact(self._sock, 4, idle_ok=True))
        if length <= MAC_LEN or length > MAX_FRAME_BYTES + MAC_LEN:
            raise ProtocolError("frame size out of range")
        body = _recv_exact(self._sock, length)
        payload, tag = body[:-MAC_LEN], body[-MAC_LEN:]
        expected = _mac(self._key, self._recv_dir, struct.pack(">Q", self._recv_counter), payload)
        if not hmac.compare_digest(expected, tag):
            raise ProtocolError("bad frame MAC (tamper, replay or reorder)")
        msg = decode_message(payload, direction=self.direction_in)
        if msg.seq != self._recv_counter:
            raise ProtocolError("unexpected sequence number")
        self._recv_counter += 1
        return msg

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _hs_json(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hs_parse(raw: bytes, expect_type: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("malformed handshake") from None
    if not isinstance(obj, dict) or obj.get("type") != expect_type or obj.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("unexpected handshake message")
    return obj


def _hex(obj: dict[str, Any], key: str, nbytes: int) -> bytes:
    value = obj.get(key)
    if not isinstance(value, str) or len(value) != nbytes * 2:
        raise ProtocolError("malformed handshake field")
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise ProtocolError("malformed handshake field") from None


def server_handshake(sock: Any, token: str) -> FrameChannel:
    """Run the server half. Raises :class:`AuthError` on a wrong token (nothing is revealed to the peer)."""
    key = _check_token(token)
    nonce = secrets.token_bytes(16)
    _write_plain(sock, _hs_json({"v": PROTOCOL_VERSION, "type": "hello", "nonce": nonce.hex()}))
    auth = _hs_parse(_read_plain(sock, MAX_HANDSHAKE_BYTES), "auth")
    cnonce = _hex(auth, "cnonce", 16)
    client_mac = _hex(auth, "mac", 32)
    if not hmac.compare_digest(client_mac, _mac(key, _LABEL, b"client|", nonce, cnonce)):
        raise AuthError("peer failed token challenge")
    _write_plain(sock, _hs_json({
        "v": PROTOCOL_VERSION, "type": "auth_ok",
        "mac": _mac(key, _LABEL, b"server|", nonce, cnonce).hex(),
    }))
    return FrameChannel(sock, _mac(key, _LABEL, b"session|", nonce, cnonce), is_server=True)


def client_handshake(sock: Any, token: str) -> FrameChannel:
    """Run the client half; also verifies the SERVER knows the token (mutual)."""
    key = _check_token(token)
    hello = _hs_parse(_read_plain(sock, MAX_HANDSHAKE_BYTES), "hello")
    nonce = _hex(hello, "nonce", 16)
    cnonce = secrets.token_bytes(16)
    _write_plain(sock, _hs_json({
        "v": PROTOCOL_VERSION, "type": "auth", "cnonce": cnonce.hex(),
        "mac": _mac(key, _LABEL, b"client|", nonce, cnonce).hex(),
    }))
    try:
        ok = _hs_parse(_read_plain(sock, MAX_HANDSHAKE_BYTES), "auth_ok")
    except (ConnectionError, ProtocolError) as exc:
        raise AuthError("server rejected the token or closed the connection") from exc
    if not hmac.compare_digest(_hex(ok, "mac", 32), _mac(key, _LABEL, b"server|", nonce, cnonce)):
        raise AuthError("server failed token challenge")
    return FrameChannel(sock, _mac(key, _LABEL, b"session|", nonce, cnonce), is_server=False)


# --------------------------------------------------------------------------- #
# Rate limiting helpers (injectable clock)
# --------------------------------------------------------------------------- #
class _TokenBucket:
    def __init__(self, rate_per_s: float, burst: int, clock: Callable[[], float]) -> None:
        self._rate, self._burst, self._clock = rate_per_s, float(burst), clock
        self._tokens = float(burst)
        self._last = clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class _AttemptLimiter:
    """At most ``max_attempts`` events per ``window_s``; used for connections and for auth failures."""

    def __init__(self, max_attempts: int, window_s: float, clock: Callable[[], float]) -> None:
        self._max, self._window, self._clock = max_attempts, window_s, clock
        self._events: list[float] = []

    def _trim(self) -> None:
        cutoff = self._clock() - self._window
        self._events = [t for t in self._events if t > cutoff]

    def record(self) -> None:
        self._trim()
        self._events.append(self._clock())

    def exceeded(self) -> bool:
        """True once ``max_attempts`` events are inside the window (used for the auth-failure lockout)."""
        self._trim()
        return len(self._events) >= self._max

    def over(self) -> bool:
        """True once MORE than ``max_attempts`` events are inside the window (used for connection rate)."""
        self._trim()
        return len(self._events) > self._max


# --------------------------------------------------------------------------- #
# Server (runner side)
# --------------------------------------------------------------------------- #
class ControlServer:
    """Listens on one literal address; serves one authenticated supervisor session at a time.

    ``handler(msg)`` is called on the session thread for each validated supervisor message. A newly authenticated
    session REPLACES an older one (a crashed supervisor leaves a half-open socket behind).
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        handler: Callable[[ControlMessage], None],
        *,
        allow_private: bool = False,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        max_messages_per_second: float = 50.0,
        message_burst: int = 100,
        max_connections_per_window: int = 20,
        connection_window_s: float = 10.0,
        max_auth_failures: int = 5,
        auth_failure_window_s: float = 60.0,
        lockout_s: float = 30.0,
    ) -> None:
        self.host = validate_address(host, allow_private=allow_private)
        self.port = port
        _check_token(token)
        self._token = token
        self._handler = handler
        self._allow_private = allow_private
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._clock = clock
        self._rate, self._burst = max_messages_per_second, message_burst
        self._conn_limiter = _AttemptLimiter(max_connections_per_window, connection_window_s, clock)
        self._fail_limiter = _AttemptLimiter(max_auth_failures, auth_failure_window_s, clock)
        self._lockout_s = lockout_s
        self._locked_until = 0.0
        self._lsock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._session: Optional[FrameChannel] = None

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> int:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name == "posix":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # never on Windows (port hijack)
            sock.bind((self.host, self.port))
            sock.listen(2)
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            raise
        self._lsock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="control-accept", daemon=True)
        self._thread.start()
        return self.port

    def close(self) -> None:
        self._closing.set()
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()
        if self._lsock is not None:
            try:
                self._lsock.close()
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    # -- runner -> supervisor --------------------------------------------- #
    def send(self, msg_type: str, data: Optional[dict[str, Any]] = None) -> bool:
        """Send to the current session. False when nobody is connected or the send failed."""
        with self._lock:
            session = self._session
        if session is None:
            return False
        try:
            session.send(msg_type, data)
            return True
        except (OSError, ConnectionError, ProtocolError):
            return False

    def connected(self) -> bool:
        with self._lock:
            return self._session is not None

    # -- internals ---------------------------------------------------------- #
    def _accept_loop(self) -> None:
        assert self._lsock is not None
        while not self._closing.is_set():
            try:
                conn, addr = self._lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, addr[0]), name="control-session", daemon=True).start()

    def _serve(self, conn: socket.socket, peer_ip: str) -> None:
        session: Optional[FrameChannel] = None
        try:
            if not _peer_allowed(peer_ip, self._allow_private):
                raise RefusedError("peer address not allowed")
            self._conn_limiter.record()
            if self._conn_limiter.over():
                raise RefusedError("too many connection attempts")
            if self._clock() < self._locked_until:
                raise RefusedError("locked out after repeated authentication failures")
            conn.settimeout(HANDSHAKE_TIMEOUT_S)
            try:
                session = server_handshake(conn, self._token)
            except AuthError:
                self._fail_limiter.record()
                if self._fail_limiter.exceeded():
                    self._locked_until = self._clock() + self._lockout_s
                raise
            conn.settimeout(1.0)
            with self._lock:
                old, self._session = self._session, session
            if old is not None:
                old.close()
            if self._on_connect:
                self._on_connect()
            self._read_loop(session, conn)
        except (ControlChannelError, OSError, ConnectionError) as exc:
            logger.debug("control session ended: %s", type(exc).__name__)  # no peer-supplied text in logs
        finally:
            was_current = False
            with self._lock:
                if session is not None and self._session is session:
                    self._session = None
                    was_current = True
            try:
                conn.close()
            except OSError:
                pass
            if was_current and self._on_disconnect:
                self._on_disconnect()

    def _read_loop(self, session: FrameChannel, conn: socket.socket) -> None:
        bucket = _TokenBucket(self._rate, self._burst, self._clock)
        while not self._closing.is_set():
            try:
                msg = session.recv()
            except socket.timeout:
                continue
            if not bucket.allow():
                raise RefusedError("message rate cap exceeded")
            self._handler(msg)


# --------------------------------------------------------------------------- #
# Client (supervisor side)
# --------------------------------------------------------------------------- #
class ControlClient:
    """Connects to a runner's control server, authenticates, and delivers runner messages to ``on_message``."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        on_message: Callable[[ControlMessage], None],
        *,
        allow_private: bool = False,
        on_close: Optional[Callable[[], None]] = None,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.host = validate_address(host, allow_private=allow_private)
        self.port = port
        _check_token(token)
        self._token = token
        self._on_message = on_message
        self._on_close = on_close
        self._timeout = connect_timeout_s
        self._channel: Optional[FrameChannel] = None
        self._thread: Optional[threading.Thread] = None
        self._closing = threading.Event()

    def connect(self) -> None:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect((self.host, self.port))
            self._channel = client_handshake(sock, self._token)
        except BaseException:
            sock.close()
            raise
        sock.settimeout(1.0)
        self._closing.clear()
        self._thread = threading.Thread(target=self._read_loop, name="control-client", daemon=True)
        self._thread.start()

    def send(self, msg_type: str, data: Optional[dict[str, Any]] = None) -> int:
        if self._channel is None:
            raise ControlChannelError("not connected")
        return self._channel.send(msg_type, data)

    def is_open(self) -> bool:
        return self._channel is not None and not self._closing.is_set()

    def close(self) -> None:
        self._closing.set()
        if self._channel is not None:
            self._channel.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _read_loop(self) -> None:
        assert self._channel is not None
        try:
            while not self._closing.is_set():
                try:
                    msg = self._channel.recv()
                except socket.timeout:
                    continue
                self._on_message(msg)
        except (ControlChannelError, OSError, ConnectionError) as exc:
            logger.debug("control client ended: %s", type(exc).__name__)
        finally:
            self._closing.set()
            if self._on_close:
                self._on_close()


# --------------------------------------------------------------------------- #
# Runner-side state machine
# --------------------------------------------------------------------------- #
@dataclass
class _Reading:
    temp_c: float


class RunnerControl:
    """What the runner obeys. Thread-safe; ``handle`` runs on the session thread, the claim loop reads it.

    Fail-closed rules:
      * no claim before ``start``; none while draining, stopped, or availability is not ``online``;
      * none while the supervisor has been gone longer than ``orphan_grace_seconds`` (a running job is NOT killed);
      * an invalid command changes nothing and is answered with an ``error`` message.
    ``stop`` and ``drain`` never abort an in-flight job. ``interrupt`` does, on purpose: it stops the running containers
    and reports each job ``host_interrupted`` (see interrupts.py), so the platform re-runs it elsewhere at once. If the
    runner cannot do that in time, the supervisor still terminates the process (the lease then expires server-side).
    """

    TEMP_STALE_SECONDS = 30.0

    def __init__(
        self,
        *,
        agent_version: str = "0",
        clock: Callable[[], float] = time.monotonic,
        orphan_grace_seconds: float = 120.0,
        on_stop: Optional[Callable[[], None]] = None,
    ) -> None:
        self._clock = clock
        self._agent_version = agent_version
        self._grace = orphan_grace_seconds
        self._on_stop = on_stop
        self._lock = threading.RLock()
        self._send: Callable[[str, Optional[dict[str, Any]]], bool] = lambda t, d=None: False
        self._started = False
        self._draining = False
        self._stopped = False
        self._availability = "online"
        self._max_concurrent = 1
        self._active: set[str] = set()
        self._completed = 0
        self._failed = 0
        self._connected = False
        self._disconnected_at: Optional[float] = None
        self._temp: Optional[tuple[float, float]] = None  # (value, time)
        self._guard: Any = None
        self._power_limit_w: Optional[float] = None
        self._t0 = clock()

    # -- wiring ------------------------------------------------------------ #
    def attach(self, server: ControlServer) -> None:
        self._send = server.send

    def bind_thermal_guard(self, guard: Any) -> None:
        self._guard = guard

    def on_connect(self) -> None:
        with self._lock:
            self._connected = True
            self._disconnected_at = None
        self._send("ready", {"agent_version": self._agent_version, "protocol": PROTOCOL_VERSION,
                             "state": "started" if self._started else "waiting"})
        self._send_usage()

    def on_disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._disconnected_at = self._clock()

    # -- supervisor commands ------------------------------------------------ #
    def handle(self, msg: ControlMessage) -> None:
        try:
            getattr(self, "_cmd_" + msg.type)(msg.data)
        except _Invalid as exc:
            self._send("error", {"code": exc.code, "message": str(exc)[:300], "ref_seq": msg.seq})
        self._send_usage()

    def _cmd_start(self, data: dict[str, Any]) -> None:
        with self._lock:
            if self._stopped:
                raise _Invalid("stopped", "runner is stopping and cannot be restarted")
            self._apply_limits({k: data[k] for k in ("max_concurrent_jobs",) if k in data})
            if "availability" in data:
                self._availability = data["availability"]
            self._started, self._draining = True, False

    def _cmd_drain(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._draining = True

    def _cmd_stop(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._stopped, self._started = True, False
        if self._on_stop:
            self._on_stop()

    def _cmd_interrupt(self, data: dict[str, Any]) -> None:
        from . import interrupts

        interrupts.REGISTRY.interrupt_all(str(data.get("reason") or "owner_needs_computer"))

    def _cmd_set_limits(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._apply_limits(data)

    def _cmd_set_availability(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._availability = data["availability"]
            if "gpu_temp_c" in data:
                self._temp = (float(data["gpu_temp_c"]), self._clock())

    def _apply_limits(self, data: dict[str, Any]) -> None:
        """Atomic: validate everything first, then apply."""
        guard = self._guard
        thermal_keys = ("gpu_temp_cap_c", "gpu_temp_resume_c", "gpu_temp_hard_stop_c")
        new_thermal: Optional[tuple[float, float, float]] = None
        if any(k in data for k in thermal_keys):
            if guard is None:
                raise _Invalid("no_thermal_guard", "this runner has no thermal guard to configure")
            cap = float(data.get("gpu_temp_cap_c", guard.cap_c))
            resume = float(data.get("gpu_temp_resume_c", guard.resume_c))
            hard = float(data.get("gpu_temp_hard_stop_c", guard.hard_stop_c))
            if not (resume < cap < hard):
                raise _Invalid("invalid_limits", "thermal limits must satisfy resume < cap < hard_stop")
            new_thermal = (cap, resume, hard)
        if new_thermal is not None:
            guard.cap_c, guard.resume_c, guard.hard_stop_c = new_thermal
        if "max_concurrent_jobs" in data:
            self._max_concurrent = int(data["max_concurrent_jobs"])
        if "gpu_power_limit_w" in data:
            self._power_limit_w = data["gpu_power_limit_w"]  # stored; applying it is the supervisor's job on Windows

    # -- what the claim loop asks -------------------------------------------- #
    def claim_block_reason(self) -> Optional[str]:
        with self._lock:
            if self._stopped:
                return "stopped"
            if not self._started:
                return "waiting_for_start"
            if self._draining:
                return "draining"
            if self._availability != "online":
                return self._availability
            if len(self._active) >= self._max_concurrent:
                return "at_capacity"
            if not self._connected and self._disconnected_at is not None \
                    and self._clock() - self._disconnected_at > self._grace:
                return "supervisor_gone"
            return None

    def may_claim(self) -> bool:
        return self.claim_block_reason() is None

    def should_stop(self) -> bool:
        with self._lock:
            return self._stopped and not self._active

    def job_started(self, job_id: str) -> None:
        with self._lock:
            self._active.add(job_id)
            count = len(self._active)
        self._send("job_started", {"job_id": str(job_id)[:128] or "unknown", "active_jobs": count})

    def job_finished(self, job_id: Optional[str], status: Optional[str]) -> None:
        with self._lock:
            self._active.discard(job_id)  # type: ignore[arg-type]
            ok = status in ("succeeded", "completed", "success")
            if ok:
                self._completed += 1
            else:
                self._failed += 1
            count, draining = len(self._active), self._draining
        self._send("job_finished", {"job_id": str(job_id or "unknown")[:128], "status": str(status or "unknown")[:32],
                                    "active_jobs": count, "draining": draining})
        self._send_usage()

    def abort_active(self) -> None:
        """A job raised out of ``run_one``: report every started job as failed so ``active`` never leaks."""
        with self._lock:
            ids = list(self._active)
        for job_id in ids:
            self.job_finished(job_id, "failed")

    def temperature_sampler(self) -> list[Any]:
        """Sampler for a ThermalGuard: the supervisor's native reading, or [] when absent or stale."""
        with self._lock:
            if self._temp is None or self._clock() - self._temp[1] > self.TEMP_STALE_SECONDS:
                return []
            return [_Reading(self._temp[0])]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_jobs": len(self._active), "draining": self._draining, "availability": self._availability,
                "started": self._started, "stopped": self._stopped, "jobs_completed": self._completed,
                "jobs_failed": self._failed, "uptime_s": max(0.0, self._clock() - self._t0),
                "max_concurrent": self._max_concurrent,
            }

    def _send_usage(self) -> None:
        self._send("usage", self.snapshot())


class _Invalid(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def start_runner_control(
    hostport: str,
    token: str,
    *,
    allow_private: bool = False,
    agent_version: str = "0",
    on_stop: Optional[Callable[[], None]] = None,
) -> tuple[RunnerControl, ControlServer]:
    """Create and start the runner-side control server. Used by ``computecoin-agent work --control-socket``."""
    host, port = parse_hostport(hostport)
    control = RunnerControl(agent_version=agent_version, on_stop=on_stop)
    server = ControlServer(host, port, token, control.handle, allow_private=allow_private,
                           on_connect=control.on_connect, on_disconnect=control.on_disconnect)
    control.attach(server)
    server.start()
    return control, server
