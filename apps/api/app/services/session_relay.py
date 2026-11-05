"""Session relay (U10): forwards opaque bytes between a host agent and a customer's browser.

The relay is deliberately dumb. Both ends connect OUTBOUND to it, so a host behind home NAT needs no open
port. Session payload is end-to-end encrypted between browser and agent (X25519 + HKDF + ChaCha20-Poly1305,
see docs/architecture/U10-interactive-sessions-design.md); the relay sees only the public keys in the two
handshake text frames, and ciphertext in binary frames. It never has a key.

``RelayBackend`` is the owner-decision seam: ``MemoryRelayBackend`` (single worker, here) can be replaced by
``RedisRelayBackend`` (several workers) or an external relay provider that implements the same interface.
Activity is measured on BINARY frames only (they are the encrypted session traffic); text control frames
(handshake, ping) do not count, so a keepalive can never hide an idle session.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Awaitable, Callable, Protocol

HOST = "host"
BROWSER = "browser"
SIDES = (HOST, BROWSER)
MAX_FRAME_BYTES = 256 * 1024


def other(side: str) -> str:
    return BROWSER if side == HOST else HOST


class RelayConn(Protocol):
    async def send(self, data: bytes | str) -> None: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class RelayBackend(Protocol):
    """What the websocket route and the session service need from a relay."""

    async def attach(self, session_id: str, side: str, conn: RelayConn) -> None:
        """Register a live connection; replaces (and closes) an older one for the same side."""

    async def detach(self, session_id: str, side: str, conn: RelayConn) -> None: ...

    async def forward(self, session_id: str, from_side: str, data: bytes | str) -> bool:
        """Send to the other side. False when it is not connected (the frame is dropped, never buffered)."""

    def peer_connected(self, session_id: str, side: str) -> bool:
        """Process-local view only (this worker's sockets). Multi-worker callers use ``peer_connected_async``."""

    def last_activity(self, session_id: str) -> float | None:
        """Process-local view only. Multi-worker callers use ``last_activity_async``."""

    async def peer_connected_async(self, session_id: str, side: str) -> bool:
        """Is that side connected to ANY worker? (Shared backends answer from shared state.)"""

    async def last_activity_async(self, session_id: str) -> float | None:
        """Wall-clock seconds of the last BINARY frame either way across all workers, or None."""

    async def close_session(self, session_id: str, reason: str) -> None: ...


class MemoryRelayBackend:
    """Process-local relay. Correct for ONE API worker only (see docs: run the relay on a single worker, or use
    a shared backend). Nothing is buffered: a frame for a peer that is not connected is dropped, and frames over
    MAX_FRAME_BYTES are refused, so a stalled peer cannot make the relay hold memory."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._conns: dict[str, dict[str, RelayConn]] = {}
        self._activity: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def attach(self, session_id: str, side: str, conn: RelayConn) -> None:
        if side not in SIDES:
            raise ValueError("bad side")
        async with self._lock:
            old = self._conns.setdefault(session_id, {}).get(side)
            self._conns[session_id][side] = conn
        if old is not None and old is not conn:
            with contextlib.suppress(Exception):
                await old.close(4000, "replaced")

    async def detach(self, session_id: str, side: str, conn: RelayConn) -> None:
        async with self._lock:
            sides = self._conns.get(session_id)
            if sides and sides.get(side) is conn:
                del sides[side]
            if sides is not None and not sides:
                self._conns.pop(session_id, None)

    async def forward(self, session_id: str, from_side: str, data: bytes | str) -> bool:
        size = len(data)
        if size > MAX_FRAME_BYTES:
            return False
        target = self._conns.get(session_id, {}).get(other(from_side))
        if target is None:
            return False
        if isinstance(data, (bytes, bytearray)):
            self._activity[session_id] = self._clock()
        try:
            await target.send(data)
        except Exception:
            return False
        return True

    def peer_connected(self, session_id: str, side: str) -> bool:
        return side in self._conns.get(session_id, {})

    def last_activity(self, session_id: str) -> float | None:
        return self._activity.get(session_id)

    def mark_activity(self, session_id: str) -> None:
        self._activity[session_id] = self._clock()

    async def peer_connected_async(self, session_id: str, side: str) -> bool:
        return self.peer_connected(session_id, side)

    async def last_activity_async(self, session_id: str) -> float | None:
        return self.last_activity(session_id)

    async def close_session(self, session_id: str, reason: str) -> None:
        async with self._lock:
            sides = self._conns.pop(session_id, {})
        self._activity.pop(session_id, None)
        for conn in sides.values():
            with contextlib.suppress(Exception):
                await conn.close(4001, reason[:100])


class RedisRelayBackend:
    """Multi-worker relay over Redis pub/sub (SESSIONS_RELAY_BACKEND=redis).

    Each (session, side) socket lives in ONE worker. That worker subscribes to the channel
    ``relay:{session_id}:{side}`` and writes what arrives to its socket. ``forward`` PUBLISHes to the OTHER side's
    channel (binary-safe) and reports whether anyone was subscribed. Frames are ciphertext in Redis, so Redis needs
    no trust, but use TLS and a private network anyway.

    Wire format of a pub/sub payload (one prefix byte): ``B`` binary frame, ``T`` text frame (utf-8),
    ``C`` close the session (rest = reason), ``R`` a newer connection took this side (rest = that connection's
    token; the holder of any other token closes with 4000).

    Presence ``relay:peer:{sid}:{side}`` has a 30 s TTL refreshed every 10 s by the owning worker; activity
    ``relay:act:{sid}`` holds the epoch of the last BINARY frame and is written at most once per second per worker.
    Nothing is buffered anywhere: a frame for a side nobody subscribes to is dropped (forward returns False).

    TODO(verify-docs): redis-py asyncio pub/sub calls (pubsub(), subscribe, listen, publish, aclose) are written
    from the redis-py 5.x documentation as remembered; check them against https://redis-py.readthedocs.io before use.
    """

    PRESENCE_TTL_SECONDS = 30
    PRESENCE_REFRESH_SECONDS = 10
    ACTIVITY_TTL_SECONDS = 86400
    ACTIVITY_WRITE_MIN_INTERVAL = 1.0

    def __init__(self, url: str, *, client: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.url = url
        self._client_obj = client
        self._clock = clock
        # (session_id, side) -> (token, conn, listener task, presence task)
        self._local: dict[tuple[str, str], tuple[str, RelayConn, asyncio.Task, asyncio.Task]] = {}
        self._act_written: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # -- plumbing ---------------------------------------------------------------------------------
    def _client(self) -> Any:
        if self._client_obj is None:
            import redis.asyncio as aioredis  # optional dependency, only for SESSIONS_RELAY_BACKEND=redis

            self._client_obj = aioredis.Redis.from_url(self.url, decode_responses=False)
        return self._client_obj

    @staticmethod
    def _chan(session_id: str, side: str) -> str:
        return f"relay:{session_id}:{side}"

    @staticmethod
    def _peer_key(session_id: str, side: str) -> str:
        return f"relay:peer:{session_id}:{side}"

    @staticmethod
    def _act_key(session_id: str) -> str:
        return f"relay:act:{session_id}"

    @staticmethod
    def _encode(data: bytes | str) -> bytes:
        if isinstance(data, (bytes, bytearray)):
            return b"B" + bytes(data)
        return b"T" + data.encode("utf-8")

    async def _listen(self, session_id: str, side: str, token: str, conn: RelayConn, ready: asyncio.Event) -> None:
        pubsub = self._client().pubsub()
        try:
            await pubsub.subscribe(self._chan(session_id, side))
            ready.set()
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                payload = msg.get("data")
                if not isinstance(payload, (bytes, bytearray)) or not payload:
                    continue
                kind, body = payload[:1], bytes(payload[1:])
                try:
                    if kind == b"B":
                        await conn.send(body)
                    elif kind == b"T":
                        await conn.send(body.decode("utf-8"))
                    elif kind == b"C":
                        await conn.close(4001, body.decode("utf-8", "replace")[:100])
                        return
                    elif kind == b"R":
                        if body.decode("ascii", "ignore") != token:
                            await conn.close(4000, "replaced")
                            return
                except Exception:
                    return  # the socket is gone; detach() cleans up the rest
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            ready.set()
            closer = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass

    async def _presence(self, session_id: str, side: str) -> None:
        key = self._peer_key(session_id, side)
        while True:
            try:
                await self._client().set(key, b"1", ex=self.PRESENCE_TTL_SECONDS)
            except Exception:
                pass  # a Redis blip must not kill the socket; the TTL covers a short outage
            await asyncio.sleep(self.PRESENCE_REFRESH_SECONDS)

    # -- RelayBackend -----------------------------------------------------------------------------
    async def attach(self, session_id: str, side: str, conn: RelayConn) -> None:
        if side not in SIDES:
            raise ValueError("bad side")
        import uuid

        token = uuid.uuid4().hex
        ready = asyncio.Event()
        listener = asyncio.create_task(self._listen(session_id, side, token, conn, ready))
        await asyncio.wait_for(ready.wait(), timeout=5)  # subscribed before anyone can forward to us
        presence = asyncio.create_task(self._presence(session_id, side))
        async with self._lock:
            old = self._local.get((session_id, side))
            self._local[(session_id, side)] = (token, conn, listener, presence)
        if old is not None and old[1] is not conn:
            with contextlib.suppress(Exception):
                await old[1].close(4000, "replaced")
            for task in (old[2], old[3]):
                task.cancel()
        # A connection of the same side on ANOTHER worker learns it was replaced.
        with contextlib.suppress(Exception):
            await self._client().publish(self._chan(session_id, side), b"R" + token.encode("ascii"))

    async def detach(self, session_id: str, side: str, conn: RelayConn) -> None:
        async with self._lock:
            cur = self._local.get((session_id, side))
            if cur is None or cur[1] is not conn:
                return
            del self._local[(session_id, side)]
        for task in (cur[2], cur[3]):
            task.cancel()
        with contextlib.suppress(Exception):
            await self._client().delete(self._peer_key(session_id, side))

    async def forward(self, session_id: str, from_side: str, data: bytes | str) -> bool:
        if len(data) > MAX_FRAME_BYTES:
            return False
        payload = self._encode(data)
        try:
            client = self._client()
            receivers = await client.publish(self._chan(session_id, other(from_side)), payload)
            if isinstance(data, (bytes, bytearray)):
                now = self._clock()
                if now - self._act_written.get(session_id, 0.0) >= self.ACTIVITY_WRITE_MIN_INTERVAL:
                    self._act_written[session_id] = now
                    await client.set(self._act_key(session_id), repr(now).encode("ascii"), ex=self.ACTIVITY_TTL_SECONDS)
        except Exception:
            return False
        return int(receivers or 0) > 0

    def peer_connected(self, session_id: str, side: str) -> bool:
        # Sync callers can only see this worker's sockets; use peer_connected_async for the shared view.
        return (session_id, side) in self._local

    def last_activity(self, session_id: str) -> float | None:
        return self._act_written.get(session_id)

    async def peer_connected_async(self, session_id: str, side: str) -> bool:
        if (session_id, side) in self._local:
            return True
        try:
            return bool(await self._client().exists(self._peer_key(session_id, side)))
        except Exception:
            return False

    async def last_activity_async(self, session_id: str) -> float | None:
        try:
            raw = await self._client().get(self._act_key(session_id))
        except Exception:
            return self._act_written.get(session_id)
        if raw is None:
            return None
        try:
            return float(raw.decode("ascii") if isinstance(raw, (bytes, bytearray)) else raw)
        except ValueError:
            return None

    async def close_session(self, session_id: str, reason: str) -> None:
        client = self._client()
        message = b"C" + reason[:100].encode("utf-8")
        for side in SIDES:
            with contextlib.suppress(Exception):
                await client.publish(self._chan(session_id, side), message)
        with contextlib.suppress(Exception):
            await client.delete(self._act_key(session_id), *(self._peer_key(session_id, s) for s in SIDES))
        self._act_written.pop(session_id, None)
        async with self._lock:
            mine = [self._local.pop((session_id, s), None) for s in SIDES]
        for entry in mine:
            if entry is not None:
                for task in (entry[2], entry[3]):
                    task.cancel()  # this worker's listener must not race the direct close below
                # The publish above reaches the OTHER workers' sockets; this worker's own sockets are closed here,
                # because their listeners were just cancelled and so would never read the "C" message.
                with contextlib.suppress(Exception):
                    await entry[1].close(4001, reason[:100])


_backend: RelayBackend | None = None


def get_relay() -> RelayBackend:
    """The process-wide relay: memory by default, Redis when SESSIONS_RELAY_BACKEND=redis (several workers)."""
    global _backend
    if _backend is None:
        from app.core.config import settings

        if (getattr(settings, "SESSIONS_RELAY_BACKEND", "memory") or "memory").lower() == "redis":
            _backend = RedisRelayBackend(settings.REDIS_URL)
        else:
            _backend = MemoryRelayBackend()
    return _backend


def set_relay(backend: RelayBackend) -> None:
    """Swap the relay (tests, or an external provider chosen by the owner)."""
    global _backend
    _backend = backend


async def pump(
    session_id: str,
    side: str,
    receive: Callable[[], Awaitable[dict]],
    relay: RelayBackend | None = None,
) -> str:
    """Read frames from one socket and forward them. ``receive`` returns a starlette websocket message dict.
    Returns why it ended ("closed" | "oversize" | "stopped")."""
    relay = relay or get_relay()
    while True:
        msg = await receive()
        if msg.get("type") == "websocket.disconnect":
            return "closed"
        data = msg.get("bytes")
        if data is None:
            data = msg.get("text")
        if data is None or data == "ping":  # keepalive text: answered by the route, never forwarded or counted
            continue
        if len(data) > MAX_FRAME_BYTES:
            return "oversize"
        await relay.forward(session_id, side, data)
