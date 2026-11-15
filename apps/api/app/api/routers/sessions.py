"""Interactive sessions (U10, docs/architecture/U10-interactive-sessions-design.md).

Customer (user auth):
  POST /v1/sessions                       create (funds a worst-case hold)
  GET  /v1/sessions, /v1/sessions/{id}    list / read
  POST /v1/sessions/{id}/connect-token    short-lived single-use browser token
  POST /v1/sessions/{id}/stop             stop and settle
  POST /v1/sessions/{id}/export           save the rental's /workspace/results to the customer's storage now
  GET  /v1/rental/templates               ready-made rental templates (public)
Host agent (machine HMAC, outbound only):
  POST /v1/agent/sessions/claim | /{id}/started | /{id}/heartbeat | /{id}/ended | /{id}/token
  Container rental (docs/architecture/RENTAL-container-rental-design.md section 7.4):
  POST /v1/agent/sessions/{id}/inputs | /export-start | /export-result | /uploads[/{upload_id}/parts/{n}/url|confirm|complete]
Relay sockets (token auth, single use):
  WS /v1/sessions/{id}/relay/host?token=...   WS /v1/sessions/{id}/relay/browser?token=...
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import decimal
import hashlib
import json
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.db.session import get_db, get_sessionmaker
from app.deps import require_verified_user
from app.core.config import settings
from app.db.base import utcnow
from app.models.data_plane import DataUpload
from app.models.provider import HardwareInventory
from app.models.sessions import SESSION_ACTIVE, SESSION_LIVE, SESSION_STARTING, STOP_HOST_ENDED, STOP_USER, ComputeSession
from app.schemas.auth import CurrentUser
from app.services import session_relay as relay_mod
from app.services import data_plane as dp
from app.services import rental as rental_svc
from app.services import sessions as svc
from app.services.audit import record_audit
from app.services.kits import get_kit
from app.services.ledger import InsufficientFunds, minor_to_str

router = APIRouter(tags=["sessions"])

WS_POLICY = 1008
IDLE_CHECK_SECONDS = 5


class InputRef(BaseModel):
    file_id: uuid.UUID
    path: str = Field(max_length=300)


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
    # Container rental (section 7.1)
    template: str | None = Field(default=None, max_length=64)
    machine_id: uuid.UUID | None = None
    workspace_gb: int = 0
    memory_mb: int | None = None
    cpus: int | None = None
    command: list[str] | None = Field(default=None, max_length=32)
    inputs: list[InputRef] = Field(default_factory=list, max_length=64)
    accept_rental_terms: bool = False


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
    rental: bool = False
    template: str | None = None
    workspace_gb: int = 0
    memory_mb: int | None = None
    cpus: int | None = None
    machine_id: uuid.UUID | None = None
    requested_machine_id: uuid.UUID | None = None
    inputs: list[dict] = Field(default_factory=list)
    export_state: str | None = None
    exports: list[dict] = Field(default_factory=list)


def _out(r: ComputeSession) -> SessionOut:
    return SessionOut(
        id=r.id, status=r.status, stop_reason=r.stop_reason, kit_ref=r.kit_ref, mode=r.mode, gpu_count=r.gpu_count,
        max_duration_seconds=r.max_duration_seconds, idle_timeout_seconds=r.idle_timeout_seconds,
        held=minor_to_str(r.held_minor), captured=None if r.captured_minor is None else minor_to_str(r.captured_minor),
        active_seconds=r.active_seconds, host_pubkey=r.host_pubkey, created_at=r.created_at.isoformat(),
        rental=bool(r.rental), template=r.template, workspace_gb=int(r.workspace_gb or 0), memory_mb=r.memory_mb,
        cpus=r.cpus, machine_id=r.machine_id, requested_machine_id=r.requested_machine_id,
        inputs=[{"path": i.get("path"), "size_bytes": i.get("size_bytes")} for i in (r.inputs or []) if isinstance(i, dict)],
        export_state=r.export_state,
        exports=[
            {"name": e.get("name"), "size_bytes": e.get("size_bytes"), "file_id": e.get("file_id")}
            for e in (r.exports or []) if isinstance(e, dict)
        ],
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
    if body.template and (body.kit_ref or body.image_digest):
        raise HTTPException(422, "template_excludes_kit_ref_and_image_digest")
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
            kit_egress=kit_egress, template=body.template, machine_id=body.machine_id,
            workspace_gb=body.workspace_gb, memory_mb=body.memory_mb, cpus=body.cpus, command=body.command,
            inputs=list(body.inputs), accept_rental_terms=body.accept_rental_terms,
        )
    except svc.SessionError as exc:
        raise _http(exc)
    except InsufficientFunds:
        raise HTTPException(402, "insufficient_credits")
    await record_audit(db, actor_user_id=user.id, action="session.create", outcome="success", resource_type="session", resource_id=str(row.id),
                       reason="rental" if row.rental else None)
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


@router.post("/v1/sessions/{session_id}/export")
async def request_export(session_id: uuid.UUID, user: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    """Ask the host to save the rental's /workspace/results to the customer's storage now."""
    row = await _mine(db, session_id, user)
    if not row.rental or row.status != SESSION_ACTIVE:
        raise HTTPException(409, "session_not_exportable")
    if row.export_state in ("requested", "running"):
        return {"export_state": row.export_state}
    now = utcnow()
    last = row.export_requested_at
    if last is not None:
        last = last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)
        if (now - last).total_seconds() < rental_svc.EXPORT_MIN_INTERVAL_SECONDS:
            raise HTTPException(429, "export_too_soon")
    row.export_requested_at = now
    row.export_state = "requested"
    await record_audit(db, actor_user_id=user.id, action="session.export", outcome="success", resource_type="session", resource_id=str(row.id))
    await db.commit()
    return {"export_state": "requested"}


@router.get("/v1/rental/templates")
async def rental_templates():
    """Ready-made rental templates that can be rented now (they have a built image). Never includes the image digest."""
    return [rental_svc.public_template(t) for t in rental_svc.available_templates()]


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
            "rental": bool(row.rental), "template": row.template, "workspace_gb": int(row.workspace_gb or 0),
            "memory_mb": row.memory_mb, "cpus": row.cpus,
            "inputs": [
                {"path": i.get("path"), "size_bytes": i.get("size_bytes"), "sha256": i.get("sha256")}
                for i in (row.inputs or []) if isinstance(i, dict)
            ],
            "export_window_seconds": int(getattr(settings, "RENTAL_EXPORT_WINDOW_SECONDS", 1800)),
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
    return {"stop": reason is not None, "reason": reason, "export": reason is None and row.export_state == "requested"}


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


# ---- container rental: files in and out (machine HMAC) ---------------------------------------
# Section 7.4 of docs/architecture/RENTAL-container-rental-design.md. The host agent moves the customer's files; the customer's
# own API credentials never reach the host. Every route is for rental sessions and for the machine that holds the session.
_SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_EXPORT_SIZE = 1 << 50


def _json_body(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(422, "invalid_body")
    if not isinstance(data, dict):
        raise HTTPException(422, "invalid_body")
    return data


def _dp_fail(exc: dp.DataPlaneError):
    raise HTTPException(status_code=exc.http_status, detail=exc.code)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


async def _rental_session(request: Request, db: AsyncSession, sid: uuid.UUID, *, files: bool):
    """Machine auth, then the session must be this machine's rental. ``files`` routes also accept a session that stopped within
    the export window (the host exports after the API ended it); other states are refused."""
    machine, raw = await authenticate_machine(request, db)
    row = (await db.execute(select(ComputeSession).where(ComputeSession.id == sid))).scalar_one_or_none()
    if row is None or row.machine_id != machine.id:
        raise HTTPException(404, "session_not_found")
    if not row.rental:
        raise HTTPException(409, "session_not_rental")
    if row.status in (SESSION_STARTING, SESSION_ACTIVE):
        return machine, row, raw
    if files:
        stopped = _aware(row.stopped_at)
        window = int(getattr(settings, "RENTAL_EXPORT_WINDOW_SECONDS", 1800))
        if stopped is not None and 0 <= (utcnow() - stopped).total_seconds() <= window:
            return machine, row, raw
        raise HTTPException(409, "export_window_closed")
    raise HTTPException(409, "session_not_live")


def _name_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _file_name(row: ComputeSession, name: str) -> str:
    return f"session/{row.id}/{name}"


def _clean_export_name(name: object) -> str:
    try:
        return rental_svc.clean_relpath(name)
    except rental_svc.RentalError:
        raise HTTPException(422, "invalid_path")


async def _session_upload(db: AsyncSession, row: ComputeSession, upload_id: uuid.UUID) -> DataUpload:
    up = await dp.get_owned_upload(db, row.customer_id, upload_id)
    if not up.idempotency_key.startswith(f"session:{row.id}:"):
        raise HTTPException(404, "Upload not found")
    return up


@router.post("/v1/agent/sessions/{session_id}/inputs")
async def session_inputs(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Fresh presigned download links for the files the customer chose as inputs. Only listed files, only while starting or active."""
    machine, row, _raw = await _rental_session(request, db, session_id, files=False)
    out = []
    for item in row.inputs or []:
        try:
            file_id = uuid.UUID(str(item.get("file_id")))
            link = await dp.create_download_link(db, owner_id=row.customer_id, file_id=file_id)
        except (ValueError, TypeError):
            raise HTTPException(409, "input_changed")
        except dp.DataPlaneError as exc:
            _dp_fail(exc)
        if link["sha256"] != item.get("sha256") or int(link["size_bytes"]) != int(item.get("size_bytes", -1)):
            raise HTTPException(409, "input_changed")
        out.append({
            "path": item["path"], "url": link["url"], "size_bytes": int(link["size_bytes"]), "sha256": link["sha256"],
            "expires_at": link["expires_at"].isoformat(),
        })
    await db.commit()
    return {"inputs": out}


@router.post("/v1/agent/sessions/{session_id}/export-start")
async def export_start(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    _machine, row, _raw = await _rental_session(request, db, session_id, files=True)
    row.export_state = "running"
    await db.commit()
    return {"state": "running"}


async def _export_budget(db: AsyncSession, row: ComputeSession, idem: str, size: int) -> None:
    """At most RENTAL_MAX_EXPORT_FILES files and about the granted workspace in bytes per session. A replay of an upload that
    already exists is not counted twice."""
    from app.models.data_plane import UPLOAD_ABORTED

    prefix = f"session:{row.id}:"
    rows = (
        await db.execute(
            select(DataUpload.idempotency_key, DataUpload.size_bytes).where(
                DataUpload.owner_id == row.customer_id, DataUpload.idempotency_key.like(prefix + "%"),
                DataUpload.status != UPLOAD_ABORTED,
            )
        )
    ).all()
    if any(key == idem for key, _size in rows):
        return
    if len(rows) >= int(getattr(settings, "RENTAL_MAX_EXPORT_FILES", 2000)):
        raise HTTPException(409, "too_many_export_files")
    cap = max(int(row.workspace_gb or 0), 1) * (1 << 30)
    if sum(int(sz) for _k, sz in rows) + size > cap:
        raise HTTPException(409, "export_too_large")


@router.post("/v1/agent/sessions/{session_id}/uploads")
async def session_start_upload(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    _machine, row, raw = await _rental_session(request, db, session_id, files=True)
    body = _json_body(raw)
    sha256, size = str(body.get("sha256") or "").lower(), body.get("size_bytes")
    if not _SHA.match(sha256) or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_EXPORT_SIZE:
        raise HTTPException(422, "invalid_hash_or_size")
    name = _clean_export_name(body.get("name"))
    file_name = _file_name(row, name)
    if len(file_name) > 255:
        raise HTTPException(422, "invalid_path")
    content_type = str(body.get("content_type") or "application/octet-stream")[:128]
    idem = f"session:{row.id}:{_name_hash(name)}:{sha256}"  # at most 126 characters: the column holds 128
    await _export_budget(db, row, idem, size)
    try:
        up, file = await dp.create_upload(
            db, owner_id=row.customer_id, idempotency_key=idem, name=file_name, content_type=content_type,
            sha256=sha256, size_bytes=size, internal_name=True,
        )
        state = await dp.upload_state(db, up)
    except dp.DataPlaneError as exc:
        _dp_fail(exc)
    await db.commit()
    return {
        "upload_id": str(up.id), "status": up.status, "part_size": up.part_size, "part_count": up.part_count,
        "file_id": str(file.id) if file is not None else (str(up.file_id) if up.file_id else None),
        "verified_parts": state["verified_parts"], "corrupt_parts": state["corrupt_parts"],
    }


@router.post("/v1/agent/sessions/{session_id}/uploads/{upload_id}/parts/{part_number}/url")
async def session_part_url(session_id: uuid.UUID, upload_id: uuid.UUID, part_number: int, request: Request, db: AsyncSession = Depends(get_db)):
    _machine, row, raw = await _rental_session(request, db, session_id, files=True)
    body = _json_body(raw)
    size = body.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool):
        raise HTTPException(422, "invalid_part_size")
    try:
        up = await _session_upload(db, row, upload_id)
        out = await dp.issue_part_url(db, up, part_number=part_number, sha256=str(body.get("sha256") or ""), size_bytes=size)
    except dp.DataPlaneError as exc:
        _dp_fail(exc)
    if "expires_at" in out:
        out["expires_at"] = out["expires_at"].isoformat()
    await db.commit()
    return out


@router.post("/v1/agent/sessions/{session_id}/uploads/{upload_id}/parts/{part_number}/confirm")
async def session_part_confirm(session_id: uuid.UUID, upload_id: uuid.UUID, part_number: int, request: Request, db: AsyncSession = Depends(get_db)):
    from fastapi.responses import JSONResponse

    from app.models.data_plane import PART_CORRUPT

    _machine, row, _raw = await _rental_session(request, db, session_id, files=True)
    try:
        up = await _session_upload(db, row, upload_id)
        part = await dp.confirm_part(db, up, part_number=part_number)
    except dp.DataPlaneError as exc:
        _dp_fail(exc)
    if part.status == PART_CORRUPT:
        await db.commit()  # keep the corrupt marker; an error response would roll it back
        return JSONResponse(status_code=409, content={"detail": "part_corrupt", "action": "resend", "part_number": part_number})
    await db.commit()
    return {"part_number": part_number, "status": part.status}


@router.post("/v1/agent/sessions/{session_id}/uploads/{upload_id}/complete")
async def session_complete(session_id: uuid.UUID, upload_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    _machine, row, _raw = await _rental_session(request, db, session_id, files=True)
    try:
        up = await _session_upload(db, row, upload_id)
        up, file = await dp.complete_upload(db, up)
    except dp.DataPlaneError as exc:
        if exc.code in dp.PERSIST_ON_ERROR:
            await db.commit()  # keep the corrupt-part marker: the error response would otherwise roll it back
        _dp_fail(exc)
    await db.commit()
    return {"upload_id": str(up.id), "status": up.status, "file_id": str(file.id) if file is not None else None,
            "sha256": up.sha256, "size_bytes": up.size_bytes}


@router.post("/v1/agent/sessions/{session_id}/export-result")
async def export_result(session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """The host reports what it saved. Each file must be one THIS session created in the customer's storage."""
    _machine, row, raw = await _rental_session(request, db, session_id, files=True)
    body = _json_body(raw)
    files = body.get("files")
    if not isinstance(files, list) or len(files) > int(getattr(settings, "RENTAL_MAX_EXPORT_FILES", 2000)):
        raise HTTPException(422, "invalid_files")
    seen: dict[str, dict] = {}
    for item in files:
        if not isinstance(item, dict):
            raise HTTPException(422, "invalid_files")
        name = _clean_export_name(item.get("name"))
        size, sha = item.get("size_bytes"), str(item.get("sha256") or "").lower()
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_EXPORT_SIZE or not _SHA.match(sha):
            raise HTTPException(422, "invalid_files")
        try:
            file_id = uuid.UUID(str(item.get("file_id")))
            f = await dp.get_owned_file(db, row.customer_id, file_id)
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_files")
        except dp.DataPlaneError:
            raise HTTPException(422, "export_file_mismatch")
        if f.name != _file_name(row, name) or f.sha256 != sha or int(f.size_bytes) != size:
            raise HTTPException(422, "export_file_mismatch")
        seen[name] = {"name": name, "size_bytes": size, "sha256": sha, "file_id": str(f.id)}
    merged = {e["name"]: e for e in (row.exports or []) if isinstance(e, dict) and e.get("name")}
    merged.update(seen)
    if len(merged) > int(getattr(settings, "RENTAL_MAX_EXPORT_FILES", 2000)):
        raise HTTPException(409, "too_many_export_files")
    row.exports = [merged[k] for k in sorted(merged)]
    row.export_state = "failed" if body.get("failed") is True else "done"
    await db.commit()
    return {"export_state": row.export_state, "files": len(row.exports)}


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
