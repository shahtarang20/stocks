"""Services / endpoints (U11, docs/architecture/U11-services-design.md).

Customer:
  POST   /v1/services                              create a service (kit, replicas, health path, spend cap)
  GET    /v1/services, /v1/services/{id}           list / view (replicas, queue depth)
  DELETE /v1/services/{id}                         stop and delete
  POST   /v1/services/{id}/invoke?path=/x          call the endpoint through the pull queue
  POST   /v1/services/{id}/v1/chat/completions     OpenAI-compatible route (kind=inference, non-streaming)
  GET    /v1/services/{id}/reconciliation          replica metered seconds vs billed amounts (read-only)
Agent (machine-signed, outbound only):
  POST   /v1/agents/services/replicas/{replica_id}/poll
  POST   /v1/agents/services/requests/{request_id}/response
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import decimal
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.api.routers.jobs import _assert_project_accessible
from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, require_verified_user
from app.models.services import (
    KIND_GENERIC, KIND_INFERENCE, REPLICA_LIVE, REQ_DONE, REQ_FAILED, REQ_QUEUED, SERVICE_DELETED, Service,
    ServiceReplica, ServiceRequest,
)
from app.schemas.auth import CurrentUser
from app.services import service_runtime as rt
from app.services.audit import record_audit
from app.services.ledger import lock_customer

router = APIRouter(prefix="/v1/services", tags=["services"])
agent_router = APIRouter(prefix="/v1/agents/services", tags=["agents-services"])

_ERR_STATUS = {"deadline_exceeded": 504, "replica_lost": 502, "service_deleted": 410, "response_too_large": 502}


class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    kind: str = KIND_GENERIC
    spec: dict[str, Any]
    project_id: uuid.UUID | None = None
    isolation_tier: str = "container"
    required_gpu_runtime: str = "cuda"
    min_replicas: int = Field(default=1, ge=0, le=64)
    max_replicas: int = Field(default=1, ge=1, le=64)
    scale_to_zero: bool = False
    port: int = Field(default=8000, ge=1, le=65535)
    health_path: str = "/health"
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    max_concurrency: int = Field(default=1, ge=1, le=64)
    max_total_cost: decimal.Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)


async def _view(db: AsyncSession, s: Service) -> dict:
    reps = (await db.execute(select(ServiceReplica).where(ServiceReplica.service_id == s.id)
                             .order_by(ServiceReplica.created_at.desc()).limit(50))).scalars().all()
    queued = await db.scalar(select(func.count()).select_from(ServiceRequest).where(
        ServiceRequest.service_id == s.id, ServiceRequest.status == REQ_QUEUED))
    return {
        "id": str(s.id), "name": s.name, "kind": s.kind, "status": s.status, "kit": s.kit_ref,
        "min_replicas": s.min_replicas, "max_replicas": s.max_replicas, "scale_to_zero": s.scale_to_zero,
        "desired_replicas": s.desired_replicas, "health_path": s.health_path, "port": s.port,
        "max_total_cost": str(s.max_total_cost), "max_concurrency": s.max_concurrency, "capped": s.capped, "last_error": s.last_error,
        "queued_requests": int(queued or 0), "invoke_url": f"/v1/services/{s.id}/invoke",
        "replicas": [{"id": str(r.id), "status": r.status, "machine_id": str(r.machine_id) if r.machine_id else None,
                      "in_flight": r.in_flight, "window_ends_at": r.window_ends_at.isoformat(),
                      "end_reason": r.end_reason} for r in reps],
    }


async def _owned(db: AsyncSession, user_id: uuid.UUID, service_id: uuid.UUID, *, lock: bool = False) -> Service:
    q = select(Service).where(Service.id == service_id, Service.customer_id == user_id, Service.status != SERVICE_DELETED)
    if lock:
        q = q.with_for_update()
    s = (await db.execute(q)).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="service_not_found")
    return s


def _raise(exc: rt.ServiceError):
    raise HTTPException(status_code=exc.http_status, detail=exc.code)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_service(body: ServiceCreate, request: Request, current: CurrentUser = Depends(require_verified_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    await lock_customer(db, current.id)
    from app.services.abuse import assert_can_submit  # U16: no-op unless ABUSE_ENFORCE=true

    await assert_can_submit(db, current.id)
    await _assert_project_accessible(db, body.project_id, current)
    try:
        s = await rt.create_service(
            db, customer_id=current.id, project_id=body.project_id, name=body.name, kind=body.kind, spec_raw=body.spec,
            isolation_tier=body.isolation_tier, required_gpu_runtime=body.required_gpu_runtime,
            min_replicas=body.min_replicas, max_replicas=body.max_replicas, scale_to_zero=body.scale_to_zero,
            port=body.port, health_path=body.health_path, request_timeout_seconds=body.request_timeout_seconds,
            max_total_cost=body.max_total_cost, max_concurrency=body.max_concurrency)
    except rt.ServiceError as exc:
        _raise(exc)
    await record_audit(db, action="service.create", outcome="success", actor_user_id=current.id, ip=client_ip(request),
                       resource_type="service", resource_id=str(s.id))
    return await _view(db, s)


@router.get("")
async def list_services(current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Service).where(Service.customer_id == current.id, Service.status != SERVICE_DELETED)
                             .order_by(Service.created_at.desc()).limit(100))).scalars().all()
    return [await _view(db, s) for s in rows]


@router.get("/{service_id}")
async def get_service(service_id: uuid.UUID, current: CurrentUser = Depends(require_verified_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    return await _view(db, await _owned(db, current.id, service_id))


@router.delete("/{service_id}")
async def delete_service(service_id: uuid.UUID, request: Request, current: CurrentUser = Depends(require_verified_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    s = await _owned(db, current.id, service_id, lock=True)
    await rt.retire_service(db, s)
    await record_audit(db, action="service.delete", outcome="success", actor_user_id=current.id, ip=client_ip(request),
                       resource_type="service", resource_id=str(s.id))
    return {"id": str(s.id), "status": s.status}


@router.get("/{service_id}/reconciliation")
async def reconciliation(service_id: uuid.UUID, current: CurrentUser = Depends(require_verified_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(Service, service_id)
    if s is None or s.customer_id != current.id:
        raise HTTPException(status_code=404, detail="service_not_found")
    return await rt.reconcile_service(db, service_id)


async def _call(db: AsyncSession, current: CurrentUser, service_id: uuid.UUID, *, path: str, method: str, body: bytes,
                content_type: str, idem: str | None, no_retry: bool, require_kind: str | None = None) -> Response:
    if not settings.SERVICES_ENABLED:
        raise HTTPException(status_code=503, detail="services_disabled")
    s = await _owned(db, current.id, service_id)
    if require_kind and s.kind != require_kind:
        raise HTTPException(status_code=404, detail="route_not_available_for_service")
    try:
        req = await rt.enqueue_request(db, s, method=method, path=path, content_type=content_type, body=body,
                                       idempotency_key=idem, no_retry=no_retry)
    except rt.ServiceError as exc:
        _raise(exc)
    req_id = req.id
    await db.commit()  # visible to the polling agents now
    deadline = time.monotonic() + int(s.request_timeout_seconds) + 1
    while True:
        await db.refresh(req)
        if req.status in (REQ_DONE, REQ_FAILED, "expired"):
            break
        if time.monotonic() >= deadline:
            break
        await db.commit()  # end the read snapshot so the next refresh sees new rows
        await asyncio.sleep(0.4)
    if req.status == REQ_DONE:
        return Response(content=rt.decode_response(req), status_code=int(req.response_status or 200),
                        media_type=req.response_content_type or "application/octet-stream",
                        headers={"X-ComputeCoin-Request-Id": str(req_id)})
    code = req.error_code or "deadline_exceeded"
    return Response(content=json.dumps({"detail": code, "request_id": str(req_id)}), media_type="application/json",
                    status_code=_ERR_STATUS.get(code, 502), headers={"X-ComputeCoin-Request-Id": str(req_id)})


@router.api_route("/{service_id}/invoke", methods=["GET", "POST"])
async def invoke(service_id: uuid.UUID, request: Request, path: str = Query(default="/"),
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                 no_retry: str | None = Header(default=None, alias="X-Service-No-Retry"),
                 current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)) -> Response:
    body = await request.body()
    return await _call(db, current, service_id, path=path, method=request.method, body=body,
                       content_type=request.headers.get("content-type", "application/json"),
                       idem=idempotency_key, no_retry=bool(no_retry))


@router.post("/{service_id}/v1/chat/completions")
async def openai_chat(service_id: uuid.UUID, request: Request,
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                      no_retry: str | None = Header(default=None, alias="X-Service-No-Retry"),
                      current: CurrentUser = Depends(require_verified_user), db: AsyncSession = Depends(get_db)) -> Response:
    body = await request.body()
    try:
        parsed = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_body")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="invalid_body")
    if parsed.get("stream"):
        raise HTTPException(status_code=422, detail="streaming_not_supported_for_services")
    return await _call(db, current, service_id, path="/v1/chat/completions", method="POST", body=body,
                       content_type="application/json", idem=idempotency_key, no_retry=bool(no_retry),
                       require_kind=KIND_INFERENCE)


# ---------------------------------------------------------------------------------------------
# Agent side
# ---------------------------------------------------------------------------------------------
def _json_body(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="invalid_body")
    return data


async def _machine_replica(db: AsyncSession, machine, replica_id: uuid.UUID) -> ServiceReplica:
    r = (await db.execute(select(ServiceReplica).where(ServiceReplica.id == replica_id).with_for_update())).scalar_one_or_none()
    if r is not None and r.machine_id is None:
        from app.models.jobs import Job

        job = await db.get(Job, r.job_id)
        if job is not None and job.machine_id == machine.id:
            r.machine_id = machine.id  # placed after the replica row was written
    if r is None or r.machine_id != machine.id:
        raise HTTPException(status_code=404, detail="replica_not_found")  # flat 404: not this machine's replica
    return r


@agent_router.post("/replicas/{replica_id}/poll")
async def poll(replica_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    machine, raw = await authenticate_machine(request, db)
    data = _json_body(raw)
    healthy = data.get("healthy") is True
    want_work = data.get("accepting", True) is True
    wait = max(0, min(int(data.get("wait_seconds") or 0), int(settings.SERVICE_POLL_MAX_WAIT_SECONDS)))
    end = time.monotonic() + wait
    while True:
        r = await _machine_replica(db, machine, replica_id)
        out = await rt.replica_poll(db, r, healthy=healthy, want_work=want_work)
        if out.get("stop") or out.get("request") is not None or time.monotonic() >= end:
            return out
        await db.commit()
        await asyncio.sleep(0.5)


@agent_router.post("/requests/{request_id}/response")
async def respond(request_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    machine, raw = await authenticate_machine(request, db)
    if len(raw) > int(settings.SERVICE_MAX_RESPONSE_BYTES) * 4 // 3 + 4096:
        raise HTTPException(status_code=413, detail="response_too_large")
    data = _json_body(raw)
    req = (await db.execute(select(ServiceRequest).where(ServiceRequest.id == request_id).with_for_update())).scalar_one_or_none()
    if req is None or req.replica_id is None:
        raise HTTPException(status_code=404, detail="request_not_found")
    replica = await _machine_replica(db, machine, req.replica_id)
    try:
        body = base64.b64decode(str(data.get("body_b64") or ""), validate=True)
        code = int(data.get("status", 200))
    except (binascii.Error, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_body")
    err = data.get("error_code")
    try:
        res = await rt.complete_request(db, req, replica, status_code=code, content_type=data.get("content_type"),
                                        body=body, error_code=str(err)[:48] if err else None)
    except rt.ServiceError as exc:
        _raise(exc)
    return {"ok": True, "result": res}
