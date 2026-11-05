"""Agent runtime v2 endpoints (U3): presigned input, checkpoint and output URLs for a RUNNING job.

All routes are machine-signed with the same HMAC scheme as the other agent routes. File bytes never pass
through the API (P4): the agent downloads and uploads straight to the data plane store with presigned URLs.
Everything is refused (fail closed) when the data plane is off. No new table: the latest checkpoint is derived
from DataFile rows named ``ckpt/<job_id>/<seq>.tar`` (design note U3-agent-runtime-v2-design.md).

Contract with U7: the job's v2 plan is read from ``job.data_policy["v2_plan"]`` by :func:`v2_plan_of`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.agents import authenticate_machine
from app.core.config import settings
from app.db.session import get_db
from app.models.data_plane import PART_CORRUPT, DataFile
from app.models.jobs import Job
from app.services import data_plane as svc
from app.services import job_fsm as fsm

router = APIRouter(prefix="/v1/agents/v2", tags=["agents-v2"])

_PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ITEM_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def v2_plan_of(job: Job) -> dict | None:
    """The job's v2 plan, or None for a v1 job. The single place U7's storage choice plugs in."""
    plan = (getattr(job, "data_policy", None) or {}).get("v2_plan")
    return plan if isinstance(plan, dict) else None


def _is_private(job: Job) -> bool:
    return (getattr(job, "isolation_tier", None) or "container").lower() in _PRIVATE_TIERS


def _ckpt_prefix(job_id: uuid.UUID) -> str:
    return f"ckpt/{job_id}/"


async def latest_checkpoint(db: AsyncSession, job: Job) -> DataFile | None:
    """Newest hash-proven checkpoint (highest zero-padded sequence in the name), any host."""
    rows = (
        await db.execute(
            select(DataFile)
            .where(
                DataFile.owner_id == job.customer_id,
                DataFile.name.like(_ckpt_prefix(job.id) + "%"),
                DataFile.deleted_at.is_(None),
            )
            .order_by(DataFile.name.desc())
            .limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


VM_IMAGE_NAME = "vm-image"  # U14: the plan-less input name under which the customer's Windows image is downloaded


def _vm_spec_of(job: Job) -> dict | None:
    """The job's ``vm`` block when it is a VM job (spec mode "vm"), else None."""
    spec = (getattr(job, "spec_v2", None) or {}).get("spec") or {}
    if spec.get("mode") != "vm":
        return None
    vm = spec.get("vm")
    return vm if isinstance(vm, dict) else {}


async def vm_image_item(db: AsyncSession, job: Job) -> dict | None:
    """U14: the customer's own image file as a download item (size and hash from the database, which must equal the
    hash the customer declared). None = unusable; the claim then carries no ``vm`` block and the agent fails closed."""
    vm = _vm_spec_of(job)
    if not vm:
        return None
    kind, _, ident = str(vm.get("image") or "").partition(":")
    try:
        fid = uuid.UUID(ident) if kind == "dataset" else None
    except ValueError:
        fid = None
    if fid is None:
        return None
    row = (await db.execute(select(DataFile).where(DataFile.id == fid, DataFile.owner_id == job.customer_id))).scalar_one_or_none()
    if row is None or row.deleted_at is not None or row.sha256 != vm.get("image_sha256"):
        return None
    return {"name": VM_IMAGE_NAME, "file_id": str(row.id), "sha256": row.sha256, "size_bytes": int(row.size_bytes)}


async def build_claim_v2(db: AsyncSession, job: Job) -> dict | None:
    """The ``v2`` object for the claim response; None for a v1 job (the claim is then unchanged).

    A private-tier job gets a v2 block only when it is a private fan-out part (U18) with the feature on, and then
    ONLY the Kit and the output names/paths (from the signed manifest): no inputs, assets, egress, checkpoint or URL
    (the plaintext input is inside ``encrypted_input``, and a private job never gets network). Anything else
    private gets none: fail closed by omission here, and the agent refuses it as well.
    """
    plan = v2_plan_of(job)
    if plan is None:
        return None
    if _is_private(job):
        if not ((job.data_policy or {}).get("private_fanout") and settings.PRIVACY_FANOUT_ENABLED and settings.privacy_active):
            return None
        if not isinstance(plan.get("kit"), dict) or not (getattr(job, "spec_v2", None) or {}).get("kit_image"):
            return None
        return {
            "spec_version": 2,
            "private": True,
            "kit": plan["kit"],
            "outputs": [
                {"name": o.get("name"), "path": o.get("path"), "required": bool(o.get("required", True))}
                for o in plan.get("outputs") or [] if isinstance(o, dict)
            ],
        }
    ck = dict(plan.get("checkpoint") or {})
    ck.pop("resume", None)
    latest = await latest_checkpoint(db, job) if ck.get("dir") else None
    if latest is not None:
        ck["resume"] = {"file_id": str(latest.id), "name": latest.name, "sha256": latest.sha256, "size_bytes": latest.size_bytes}
    byoc = {"byoc": plan["byoc"]} if isinstance(plan.get("byoc"), dict) else {}  # U8
    service = {"service": plan["service"]} if isinstance(plan.get("service"), dict) else {}  # U11
    vm_block: dict = {}
    vm = _vm_spec_of(job)
    if vm is not None:  # U14: never run a VM job as a batch job; no usable vm block means the agent refuses it
        item = await vm_image_item(db, job) if settings.WINDOWS_VM_ENABLED else None
        vm_block = {"mode": "vm", "vm": None if item is None else {
            "image": item, "vcpus": vm.get("vcpus", 4), "memory_gb": vm.get("memory_gb", 16),
            "gpu_passthrough": bool(vm.get("gpu_passthrough", True)),
            "command": vm.get("command") if isinstance(vm.get("command"), list) else None,
            "params": ((job.spec_v2 or {}).get("spec") or {}).get("params") or {},
        }}
    return {
        **byoc,
        **service,
        **vm_block,
        "spec_version": 2,
        "kit": plan.get("kit"),
        "inputs": plan.get("inputs") or [],
        "assets": plan.get("assets") or [],
        "outputs": plan.get("outputs") or [],
        "egress": plan.get("egress") or [],
        "checkpoint": ck or None,
    }


async def _running_job(db: AsyncSession, machine, job_id: uuid.UUID, *, allow_private: bool = False) -> tuple[Job, dict]:
    """The job must be RUNNING on THIS machine with a v2 plan; anything else is a flat 404.

    ``allow_private`` (output upload routes only) admits a private fan-out part (U18) when the feature is on: the
    agent uploads its already SEALED output, which is ciphertext to the store and the API.
    """
    job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    plan = v2_plan_of(job) if job is not None else None
    if job is None or plan is None or job.machine_id != machine.id or job.status != fsm.RUNNING:
        raise HTTPException(status_code=404, detail="Job not found")
    if _is_private(job) and not (
        allow_private and (job.data_policy or {}).get("private_fanout") and settings.PRIVACY_FANOUT_ENABLED and settings.privacy_active
    ):
        raise HTTPException(status_code=404, detail="Job not found")
    return job, plan


def _body(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="invalid_body")
    return data


def _fail(exc: svc.DataPlaneError):
    raise HTTPException(status_code=exc.http_status, detail=exc.code)


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="invalid_id")


@router.post("/jobs/{job_id}/download-url")
async def download_url(job_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    machine, raw = await authenticate_machine(request, db)
    job, plan = await _running_job(db, machine, job_id)
    body = _body(raw)
    kind, name = body.get("kind"), body.get("name")
    owner_id = job.customer_id
    if kind in ("input", "asset"):
        items = plan.get("inputs" if kind == "input" else "assets") or []
        if kind == "input" and name == VM_IMAGE_NAME:  # U14: the VM job's own image, resolved from the job spec (not the plan)
            vm_item = await vm_image_item(db, job) if settings.WINDOWS_VM_ENABLED else None
            items = [vm_item] if vm_item is not None else []
        item = next((i for i in items if isinstance(i, dict) and i.get("name") == name), None)
        if item is None:
            raise HTTPException(status_code=404, detail="not_in_plan")  # only planned items, never an arbitrary id
        file_id = _uuid(item.get("file_id"))
        if kind == "asset":
            owner_id = _uuid(item.get("owner_id"))
    elif kind == "checkpoint":
        latest = await latest_checkpoint(db, job)
        if latest is None or str(latest.id) != str(name):
            raise HTTPException(status_code=404, detail="not_in_plan")
        file_id = latest.id
    else:
        raise HTTPException(status_code=422, detail="invalid_kind")
    try:
        link = await svc.create_download_link(db, owner_id=owner_id, file_id=file_id)
    except svc.DataPlaneError as exc:
        _fail(exc)
    return {"url": link["url"], "expires_at": link["expires_at"].isoformat(), "sha256": link["sha256"], "size_bytes": link["size_bytes"]}


async def _job_upload(db: AsyncSession, job: Job, upload_id: uuid.UUID):
    up = await svc.get_owned_upload(db, job.customer_id, upload_id)
    if not up.idempotency_key.startswith(f"job:{job.id}:"):
        raise HTTPException(status_code=404, detail="Upload not found")
    return up


@router.post("/jobs/{job_id}/uploads")
async def start_upload(job_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    machine, raw = await authenticate_machine(request, db)
    job, plan = await _running_job(db, machine, job_id, allow_private=True)
    body = _body(raw)
    purpose, sha256, size = body.get("purpose"), str(body.get("sha256") or "").lower(), body.get("size_bytes")
    if not _SHA.match(sha256) or not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise HTTPException(status_code=422, detail="invalid_hash_or_size")
    content_type = str(body.get("content_type") or "application/octet-stream")
    if purpose == "output":
        name = str(body.get("name") or "")
        declared = {o.get("name") for o in (plan.get("outputs") or []) if isinstance(o, dict)}
        if not _ITEM_NAME.match(name) or name not in declared:
            raise HTTPException(status_code=422, detail="output_not_declared")  # paths and names come from the Kit
        file_name, idem = f"out/{job.id}/{name}", f"job:{job.id}:out:{name}:{sha256}"
    elif purpose == "checkpoint":
        ck = plan.get("checkpoint") or {}
        seq = body.get("seq")
        if not ck.get("dir") or not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < 10**8:
            raise HTTPException(status_code=422, detail="checkpoint_not_declared")
        file_name, idem = f"{_ckpt_prefix(job.id)}{seq:08d}.tar", f"job:{job.id}:ckpt:{seq}:{sha256}"
    else:
        raise HTTPException(status_code=422, detail="invalid_purpose")
    try:
        up, file = await svc.create_upload(
            db, owner_id=job.customer_id, idempotency_key=idem, name=file_name, content_type=content_type,
            sha256=sha256, size_bytes=size, retention_days=body.get("retention_days") if isinstance(body.get("retention_days"), int) else None,
            internal_name=True,  # "out/<job>/<name>" and "ckpt/<job>/<seq>.tar" are platform-built names; "/" is the namespace separator
        )
        state = await svc.upload_state(db, up)
    except svc.DataPlaneError as exc:
        _fail(exc)
    return {
        "upload_id": str(up.id), "status": up.status, "part_size": up.part_size, "part_count": up.part_count,
        "file_id": str(file.id) if file is not None else (str(up.file_id) if up.file_id else None),
        "verified_parts": state["verified_parts"], "corrupt_parts": state["corrupt_parts"],
    }


@router.post("/jobs/{job_id}/uploads/{upload_id}/parts/{part_number}/url")
async def part_url(job_id: uuid.UUID, upload_id: uuid.UUID, part_number: int, request: Request, db: AsyncSession = Depends(get_db)):
    machine, raw = await authenticate_machine(request, db)
    job, _ = await _running_job(db, machine, job_id, allow_private=True)
    body = _body(raw)
    size = body.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool):
        raise HTTPException(status_code=422, detail="invalid_part_size")
    try:
        up = await _job_upload(db, job, upload_id)
        out = await svc.issue_part_url(db, up, part_number=part_number, sha256=str(body.get("sha256") or ""), size_bytes=size)
    except svc.DataPlaneError as exc:
        _fail(exc)
    if "expires_at" in out:
        out["expires_at"] = out["expires_at"].isoformat()
    return out


@router.post("/jobs/{job_id}/uploads/{upload_id}/parts/{part_number}/confirm")
async def part_confirm(job_id: uuid.UUID, upload_id: uuid.UUID, part_number: int, request: Request, db: AsyncSession = Depends(get_db)):
    machine, _ = await authenticate_machine(request, db)
    job, _plan = await _running_job(db, machine, job_id, allow_private=True)
    try:
        up = await _job_upload(db, job, upload_id)
        part = await svc.confirm_part(db, up, part_number=part_number)
    except svc.DataPlaneError as exc:
        _fail(exc)
    if part.status == PART_CORRUPT:
        await db.commit()  # keep the corrupt marker; an error response would roll it back
        return JSONResponse(status_code=409, content={"detail": "part_corrupt", "action": "resend", "part_number": part_number})
    return {"part_number": part_number, "status": part.status}


@router.post("/jobs/{job_id}/uploads/{upload_id}/complete")
async def complete(job_id: uuid.UUID, upload_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    machine, _ = await authenticate_machine(request, db)
    job, _plan = await _running_job(db, machine, job_id, allow_private=True)
    try:
        up = await _job_upload(db, job, upload_id)
        up, file = await svc.complete_upload(db, up)
    except svc.DataPlaneError as exc:
        if exc.code in svc.PERSIST_ON_ERROR:
            await db.commit()  # keep the corrupt-part marker: the error response would otherwise roll it back
        _fail(exc)
    return {"upload_id": str(up.id), "status": up.status, "file_id": str(file.id) if file is not None else None,
            "sha256": up.sha256, "size_bytes": up.size_bytes}
