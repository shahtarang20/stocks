"""U13 routes: admin datacenter_group label, agent benchmark plan and report. Written, never run."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.deps import require_roles
from app.schemas.auth import CurrentUser
from app.db.base import utcnow
from app.db.session import get_db
from app.models.provider import ProviderMachine
from app.services import multi_node as svc

router = APIRouter(prefix="/v1/agents/benchmark", tags=["multi-node"])
admin_router = APIRouter(prefix="/v1/admin/machines", tags=["multi-node-admin"])


@router.get("/plan")
async def plan(request: Request, db: AsyncSession = Depends(get_db)):
    machine, _ = await authenticate_machine(request, db)
    return {"peers": await svc.benchmark_plan(db, machine, utcnow())}


@router.post("/report")
async def report(request: Request, db: AsyncSession = Depends(get_db)):
    machine, raw = await authenticate_machine(request, db)
    try:
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid_body")
    try:
        n = await svc.store_benchmark(db, machine, body, utcnow())
    except svc.MultiNodeError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.code)
    await db.commit()
    return {"stored": n}


@admin_router.put("/{machine_id}/datacenter-group")
async def set_datacenter_group(
    machine_id: uuid.UUID,
    body: dict,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
):
    """OWNER DECISION which hosts qualify. ``{"datacenter_group": "<label>"}`` sets it, ``null`` clears it. Default none."""
    label = body.get("datacenter_group")
    if label is not None and not svc.valid_label(label):
        raise HTTPException(status_code=422, detail="invalid_label")
    m = (await db.execute(select(ProviderMachine).where(ProviderMachine.id == machine_id))).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="machine_not_found")
    m.datacenter_group = label
    await db.commit()
    return {"machine_id": str(m.id), "datacenter_group": m.datacenter_group}
