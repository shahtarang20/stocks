"""Interactive sessions (U10, docs/architecture/U10-interactive-sessions-design.md).

Customer (user auth):
  POST /v1/sessions                       create (funds a worst-case hold)
  GET  /v1/sessions, /v1/sessions/{id}    list / read
  POST /v1/sessions/{id}/connect-token    short-lived single-use browser token
  POST /v1/sessions/{id}/stop             stop and settle
Host agent (machine HMAC, outbound only):
  POST /v1/agent/sessions/claim | /{id}/started | /{id}/heartbeat | /{id}/ended | /{id}/token
Relay sockets (token auth, single use):
  WS /v1/sessions/{id}/relay/host?token=...   WS /v1/sessions/{id}/relay/browser?token=...
"""

from __future__ import annotations

import asyncio
import contextlib
import decimal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.db.session import get_db, get_sessionmaker
from app.deps import require_verified_user
from app.models.provider import HardwareInventory
from app.models.sessions import SESSION_ACTIVE, SESSION_LIVE, SESSION_STARTING, STOP_HOST_ENDED, STOP_USER, ComputeSession
from app.schemas.auth import CurrentUser
from app.services import session_relay as relay_mod
from app.services import sessions as svc
from app.services.audit import record_audit
from app.services.kits import get_kit
from app.services.ledger import InsufficientFunds, minor_to_str

router = APIRouter(tags=["sessions"])

WS_POLICY = 1008
IDLE_CHECK_SECONDS = 5


class SessionCreate(BaseModel):
    kit_ref: str | None = Field(default=None, max_length=128, description="slug@version")
    image_digest: str | None = Field(default=None, max_length=255)
    gpu_count: int = 1
    max_price_per_gpu_hour: decimal.Decimal = Field(max_digits=18)  # max_digits also bounds the exponent: 1e400 overflowed (500)
    max_duration_seconds: int = 3600
    idle_timeout_seconds: int = 900
    client_pubkey: str = Field(description="browser ephemeral X25519 public key, 64 hex chars")
    isolation_tier: str = "container"
    network_policy: dict | None = None
    mode: str = "pty"
    tcp_port: int | None = None
    project_id: uuid.UUID | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    status: str
    stop_reason: str | None = None
    kit_ref: str | None = None
    mode: str
    gpu_count: int
    max_duration_seconds: int
    idle_timeout_seconds: int
    held: str
    captured: str | None = None
    active_seconds: int | None = None
    host_pubkey: str | None = None
    created_at: str


def _out(r: ComputeSession) -> SessionOut:
    return SessionOut(
        id=r.id, status=r.status, stop_reason=r.stop_reason, kit_ref=r.kit_ref, mode=r.mode, gpu_count=r.gpu_count,
        max_duration_seconds=r.max_duration_seconds, idle_timeout_seconds=r.idle_timeout_seconds,
        held=minor_to_str(r.held_minor), captured=None if r.captured_minor is None else minor_to_str(r.captured_minor),
        active_seconds=r.active_seconds, host_pubkey=r.host_pubkey, created_at=r.created_at.isoformat(),
    )


def _http(exc: svc.SessionError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.code)


async def _mine(db: AsyncSession, sid: uuid.UUID, user: CurrentUser) -> ComputeSession:
    row = (await db.execute(select(ComputeSession).where(ComputeSession.id == sid))).scalar_one_or_none()
    if row is None or row.customer_id != user.id:
        raise HTTPException(404, "session_not_found")
    return row


@router.post("/v1/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    body: SessionCreate, request: Request, user: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    from app.services.abuse import assert_can_submit  # U16: no-op unless ABUSE_ENFORCE=true

    await assert_can_submit(db, user.id)
    kit_image = None
    kit_session = None
    kit_egress = None
    if body.kit_ref:
        slug, _, version = body.kit_ref.partition("@")
        kit = await get_kit(db, slug, version) if version else None
        if kit is None or kit.status != "published":
            raise HTTPException(404, "kit_not_found")
        if (kit.manifest or {}).get("mode") != "session":
            raise HTTPException(422, "kit_mode_not_session")  # batch and service kits never start as sessions
        kit_image = kit.image_digest
        kit_session = (kit.manifest or {}).get("session")
        eg = (kit.manifest or {}).get("egress")
        kit_egress = eg if isinstance(eg, list) else None
    try:
        row = await svc.create_session(
            db, customer_id=user.id, project_id=body.project_id, kit_ref=body.kit_ref, kit_image=kit_image,
            kit_session=kit_session if isinstance(kit_session, dict) else None, image_digest=body.image_digest,
            gpu_count=body.gpu_count, max_price_per_gpu_hour=body.max_price_per_gpu_hour,
            max_duration_seconds=body.max_duration_seconds, idle_timeout_seconds=body.idle_timeout_seconds,
            client_pubkey=body.client_pubkey, isolation_tier=body.isolation_tier,
            network_policy=body.network_policy, mode=body.mode, tcp_port=body.tcp_port,
            kit_egress=kit_egress,
        )
    except svc.SessionError as exc:
        raise _http(exc)
    except InsufficientFunds:
        raise HTTPException(402, "insufficient_credits")
    await record_audit(db, actor_user_id=user.id, action="session.create", outcome="success", resource_type="session", resource_id=str(row.id))
    await db.commit()
    return _out(row)


@router.get("/v1/sessions", response_model=list[SessionOut])
async def list_sessions(user: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ComputeSession).where(ComputeSession.customer_id == user.id)
            .order_by(ComputeSession.created_at.desc()).limit(100)
        )
    ).scalars().all()
    return [_out(r) for r in rows]


@router.get("/v1/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, user: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    row = await _mine(db, session_id, user)
    if row.status in SESSION_LIVE:  # lazy enforcement, in case no worker reaper is running
        await svc.reap(db, relay_mod.get_relay())
        await db.commit()
        await db.refresh(row)
    return _out(row)


@router.post("/v1/sessions/{session_id}/connect-token")
async def connect_token(session_id: uuid.UUID, user: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    row = await _mine(db, session_id, user)
    try:
        token = await svc.issue_browser_token(db, row)
    except svc.SessionError as exc:
        raise _http(exc)
    await db.commit()
    return {"token": token, "expires_in": svc.TOKEN_TTL_SECONDS, "host_pubkey": row.host_pubkey, "status": row.status}


@router.post("/v1/sessions/{session_id}/stop", response_model=SessionOut)
async def stop(session_id: uuid.UUID, user: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    row = await _mine(db, session_id, user)
    await svc.stop_session(db, row, STOP_USER)
    await record_audit(db, actor_user_id=user.id, action="session.stop", outcome="success", resource_type="session", resource_id=str(row.id))
    await db.commit()
    await relay_mod.get_relay().close_session(str(row.id), STOP_USER)
    return _out(row)


# ---- host agent (machine HMAC) -------------------------------------------------------------
class ClaimBody(BaseModel):
    host_pubkey: str


async def _machine_session(request: Request, db: AsyncSession, sid: uuid.UUID):
    machine, raw = await authenticate_machine(request, db)
    row = (await db.execute(select(ComputeSession).where(ComputeSession.id == sid))).scalar_one_or_none()
    if row is None or row.machine_id != machine.id:
        raise HTTPException(404, "session_not_found")
    return machine, row


@router.post("/v1/agent/sessions/claim")
async def claim(request: Request, db: AsyncSession = Depends(get_db)):
    machine, raw = await authenticate_machine(request, db)
    import json

    try:
        body = ClaimBody(**json.loads(raw or b"{}"))
    except Exception:
        raise HTTPException(422, "invalid_body")
    gpus = (
        await db.execute(select(HardwareInventory.gpu_count).where(HardwareInventory.machine_id == machine.id))
    ).scalar_one_or_none() or 0
    try:
        got = await svc.claim_next(db, machine, host_pubkey=body.host_pubkey, machine_gpus=int(gpus))
    except svc.SessionError as exc:
        raise _http(exc)
    await db.commit()
    if got is None:
        return {"session": None}
    row, token = got
    return {
        "session": {
            "id": str(row.id), "image_digest": row.image_digest, "mode": row.mode, "tcp_port": row.tcp_port,
            "command": row.command, "gpu_count": row.gpu_count, "isolation_tier": row.isolation_tier,
            "network_policy": row.network_policy, "max_duration_seconds": row.max_duration_seconds,
            "client_pubkey": row.client_pubkey, "host_token": token, "token_expires_in": svc.TOKEN_TTL_SECONDS,
        }
    }


@router.post("/v1/agent/sessions/{session_id}/token")
async def host_token(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    _, row = await _machine_session(request, db, session_id)
    if row.status not in SESSION_LIVE:
        raise HTTPException(409, "session_not_live")
    token = await svc.refresh_host_token(db, row)
    await db.commit()
    return {"host_token": token, "token_expires_in": svc.TOKEN_TTL_SECONDS}


@router.post("/v1/agent/sessions/{session_id}/started")
async def started(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    _, row = await _machine_session(request, db, session_id)
    await svc.mark_active(db, row)
    await db.commit()
    return {"status": row.status}


@router.post("/v1/agent/sessions/{session_id}/heartbeat")
async def host_heartbeat(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    _, row = await _machine_session(request, db, session_id)
    relay = relay_mod.get_relay()
    reason = await svc.heartbeat(db, row, await relay.last_activity_async(str(row.id)))
    await db.commit()
    if reason:
        await relay.close_session(str(row.id), reason)
    return {"stop": reason is not None, "reason": reason}


class EndedBody(BaseModel):
    failed: bool = False


@router.post("/v1/agent/sessions/{session_id}/ended")
async def ended(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    import json

    _, row = await _machine_session(request, db, session_id)
    raw = await request.body()
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        raise HTTPException(422, "invalid_body")
    if not isinstance(parsed, dict):
        raise HTTPException(422, "invalid_body")
    failed = bool(parsed.get("failed"))
    await svc.stop_session(db, row, svc.STOP_START_FAILED if failed and row.started_at is None else STOP_HOST_ENDED, failed=failed)
    await db.commit()
    await relay_mod.get_relay().close_session(str(row.id), "ended")
    return {"status": row.status}


# ---- relay sockets ---------------------------------------------------------------------------
class _WsConn:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def send(self, data: bytes | str) -> None:
        if isinstance(data, (bytes, bytearray)):
            await self.ws.send_bytes(bytes(data))
        else:
            await self.ws.send_text(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.ws.close(code=code, reason=reason)


async def _relay_socket(ws: WebSocket, session_id: uuid.UUID, side: str, token: str) -> None:
    maker = get_sessionmaker()
    async with maker() as db:
        row = await svc.consume_token(db, session_id, side, token)
    if row is None:
        await ws.close(code=WS_POLICY, reason="unauthorized")
        return
    await ws.accept()
    sid = str(session_id)
    relay = relay_mod.get_relay()
    conn = _WsConn(ws)
    await relay.attach(sid, side, conn)

    async def watchdog() -> None:  # enforce idle / max duration even when nobody calls the API
        while True:
            await asyncio.sleep(IDLE_CHECK_SECONDS)
            async with maker() as wdb:
                if await svc.reap(wdb, relay):
                    await wdb.commit()
                cur = (await wdb.execute(select(ComputeSession.status).where(ComputeSession.id == session_id))).scalar_one_or_none()
            if cur not in SESSION_LIVE:
                await relay.close_session(sid, "stopped")
                return

    wd = asyncio.create_task(watchdog())
    try:
        await ws.send_text('{"type":"connected","peer":%s}' % ("true" if await relay.peer_connected_async(sid, relay_mod.other(side)) else "false"))
        await relay_mod.pump(sid, side, ws.receive, relay)
    except Exception:
        pass
    finally:
        wd.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await wd
        await relay.detach(sid, side, conn)
        with contextlib.suppress(Exception):
            await ws.close()


@router.websocket("/v1/sessions/{session_id}/relay/host")
async def relay_host(ws: WebSocket, session_id: uuid.UUID, token: str = Query("")) -> None:
    await _relay_socket(ws, session_id, relay_mod.HOST, token)


@router.websocket("/v1/sessions/{session_id}/relay/browser")
async def relay_browser(ws: WebSocket, session_id: uuid.UUID, token: str = Query("")) -> None:
    await _relay_socket(ws, session_id, relay_mod.BROWSER, token)
