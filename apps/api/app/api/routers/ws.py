"""WebSocket job push for enrolled agents (Phase B5).

Why this exists
---------------
Polling ``POST /v1/agents/jobs/claim`` works and must keep working — it is the
only path that survives a proxy that drops upgrades, and it is what every
already-deployed agent uses. This endpoint is an *optimisation*: a connected
agent learns about a job in milliseconds instead of on the next poll tick. It
never becomes the source of truth. A pushed ``job_available`` frame is a hint;
the agent still claims through the ordinary HTTP endpoint, so the scheduler's
locking and idempotency rules are untouched.

Authentication
--------------
Exactly the machine HMAC scheme from :mod:`app.core.machine_auth` that the
heartbeat uses — same canonical string, same signing key, same skew window. A
WebSocket handshake carries no body, so the body digest is that of ``b""``.
Browsers cannot set handshake headers, and agents are not browsers, so both
header and query-string presentation of the same signature are accepted; the
material signed is identical either way. Nothing weaker is offered.

Failure is a flat close with code 1008 *before* ``accept()``, so an unauthorised
caller never reaches an established socket and learns nothing from the error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.machine_auth import build_canonical, verify_signature
from app.db.base import utcnow
from app.db.session import get_db
from app.models.provider import ProviderMachine

logger = logging.getLogger("computecoin.ws")

router = APIRouter(prefix="/v1/agents", tags=["agent-ws"])

# Close codes (RFC 6455 private range / policy violation).
WS_POLICY_VIOLATION = 1008
WS_NORMAL_CLOSURE = 1000

# Bound the per-connection outbound queue. A wedged agent that stops reading
# must not grow the server's heap; we drop the oldest hint instead, because a
# stale "a job is waiting" is worthless anyway (polling still covers it).
_SEND_QUEUE_MAXSIZE = 64


class MachineConnectionRegistry:
    """In-process registry of live agent sockets, keyed by machine id.

    In-process is deliberate and sufficient: a push is a best-effort hint, and
    an agent connected to another worker still polls. Making this cross-worker
    (Redis pub/sub) would add a failure mode for no correctness gain.
    """

    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def register(self, machine_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._queues.setdefault(machine_id, set()).add(queue)

    async def unregister(self, machine_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            bucket = self._queues.get(machine_id)
            if bucket is None:
                return
            bucket.discard(queue)
            if not bucket:
                self._queues.pop(machine_id, None)

    def connection_count(self, machine_id: str) -> int:
        return len(self._queues.get(machine_id, ()))

    def connected_machines(self) -> list[str]:
        return sorted(self._queues)

    async def push(self, machine_id: str, payload: dict[str, Any]) -> int:
        """Enqueue ``payload`` for every socket of ``machine_id``.

        Returns the number of sockets the message was queued to (0 means the
        machine is not connected here — the caller should just rely on polling).
        Delivery is strictly scoped to the one machine: no broadcast API exists,
        so one provider's job hints can never land on another's socket.
        """
        async with self._lock:
            targets = list(self._queues.get(machine_id, ()))
        delivered = 0
        for q in targets:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop oldest; hints are not durable messages
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)
                delivered += 1
        return delivered

    def clear(self) -> None:
        """Test/shutdown helper — drop all registrations."""
        self._queues.clear()


registry = MachineConnectionRegistry()


async def push_job_available(
    machine_id: uuid.UUID | str,
    *,
    job_id: uuid.UUID | str | None = None,
    reason: str = "job_available",
) -> int:
    """Hint one machine that work is waiting. Safe to call and ignore.

    Returns the number of sockets notified. Never raises: a push failure must
    not roll back the scheduling transaction that triggered it.
    """
    if not settings.WS_JOB_PUSH_ENABLED:
        return 0
    try:
        return await registry.push(
            str(machine_id),
            {
                "type": reason,
                "job_id": str(job_id) if job_id is not None else None,
                "ts": int(utcnow().timestamp()),
            },
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("ws push failed for machine %s", machine_id)
        return 0


def _enqueue(queue: asyncio.Queue, payload: dict[str, Any]) -> None:
    """Non-blocking enqueue: a wedged reader must never stall the read loop."""
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(payload)


def _first(websocket: WebSocket, header: str, param: str) -> str:
    """Read a credential from a handshake header, falling back to the query."""
    value = websocket.headers.get(header)
    if value:
        return value
    return websocket.query_params.get(param, "")


async def authenticate_machine_ws(
    websocket: WebSocket, db: AsyncSession
) -> ProviderMachine | None:
    """Verify the machine HMAC on a handshake. ``None`` means reject.

    The canonical string is ``machine_id\\n timestamp \\n sha256(b"")`` — the same
    builder the heartbeat uses, with the empty body a handshake necessarily has.
    """
    machine_header = _first(websocket, "X-ComputeCoin-Machine", "machine_id")
    ts_header = _first(websocket, "X-ComputeCoin-Timestamp", "ts")
    sig_header = _first(websocket, "X-ComputeCoin-Signature", "signature")

    try:
        machine_uuid = uuid.UUID(machine_header)
    except (ValueError, TypeError):
        return None

    try:
        ts = int(ts_header)
    except (ValueError, TypeError):
        return None
    if abs(int(utcnow().timestamp()) - ts) > settings.MACHINE_HMAC_SKEW_SECONDS:
        return None

    machine = (
        await db.execute(select(ProviderMachine).where(ProviderMachine.id == machine_uuid))
    ).scalar_one_or_none()
    if machine is None or machine.status == "deregistered" or not machine.signing_key:
        return None

    canonical = build_canonical(machine_header, ts_header, b"")
    if not verify_signature(machine.signing_key, canonical, sig_header):
        return None
    return machine


@router.websocket("/ws")
async def agent_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Authenticated agent socket: receives job hints, answers pings.

    Protocol (JSON frames both ways):
      server -> ``{"type":"connected","machine_id":...}`` on accept,
                ``{"type":"pong","ts":...}`` to a ping,
                ``{"type":"job_available","job_id":...,"ts":...}`` as a hint.
      client -> ``{"type":"ping"}`` keepalive, ``{"type":"close"}`` clean exit.
    """
    if not settings.WS_JOB_PUSH_ENABLED:
        # Feature-flagged off: refuse before accept so no socket is established.
        await websocket.close(code=WS_POLICY_VIOLATION, reason="ws_disabled")
        return

    machine = await authenticate_machine_ws(websocket, db)
    if machine is None:
        await websocket.close(code=WS_POLICY_VIOLATION, reason="unauthorized")
        return

    machine_id = str(machine.id)
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SEND_QUEUE_MAXSIZE)
    await websocket.accept()
    await registry.register(machine_id, queue)

    async def _writer() -> None:
        """Single owner of outbound sends, so a push from another task never
        interleaves with a reply mid-frame."""
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)

    writer = asyncio.create_task(_writer())
    try:
        await websocket.send_json(
            {"type": "connected", "machine_id": machine_id, "ts": int(utcnow().timestamp())}
        )
        while True:
            try:
                message = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except (ValueError, TypeError):
                # Unparseable frame: tell the agent, keep the socket.
                _enqueue(queue, {"type": "error", "detail": "invalid_json"})
                continue
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "ping":
                _enqueue(queue, {"type": "pong", "ts": int(utcnow().timestamp())})
            elif kind == "close":
                break
            else:
                _enqueue(queue, {"type": "error", "detail": "unknown_frame"})
    except WebSocketDisconnect:
        pass
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer
        await registry.unregister(machine_id, queue)
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_NORMAL_CLOSURE)
