"""Admin: register workload image digests per GPU runtime (P3.4).

POST /v1/admin/model-versions/{version_tag}/images  {"gpu_runtime": "cuda"|"rocm", "image_digest": "name@sha256:..."}
Writes the ModelVersionImage row and an IMAGE_HASH transparency-log entry. Values come from
``workloads/digests.json`` (written by CI).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import client_ip, require_roles
from app.schemas.auth import CurrentUser
from app.services.audit import record_audit
from app.services.workload_images import ImageRegistrationError, register_image

router = APIRouter(prefix="/v1/admin", tags=["admin-workloads"])


class RegisterImageRequest(BaseModel):
    gpu_runtime: str = Field(pattern="^(cuda|rocm)$")
    image_digest: str = Field(min_length=71, max_length=255)
    weights_manifest_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class RegisterImageResponse(BaseModel):
    version_tag: str
    gpu_runtime: str
    image_digest: str
    log_leaf_index: int


@router.post("/model-versions/{version_tag}/images", response_model=RegisterImageResponse)
async def register_model_version_image(
    version_tag: str,
    body: RegisterImageRequest,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> RegisterImageResponse:
    try:
        row, leaf_index = await register_image(
            db,
            version_tag=version_tag,
            gpu_runtime=body.gpu_runtime,
            image_digest=body.image_digest,
            weights_manifest_sha256=body.weights_manifest_sha256,
        )
    except ImageRegistrationError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.code)
    await record_audit(
        db, action="model_image.register", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="model_version", resource_id=version_tag,
        reason=f"{body.gpu_runtime}:{body.image_digest}",
    )
    await db.flush()
    return RegisterImageResponse(
        version_tag=version_tag,
        gpu_runtime=row.gpu_runtime,
        image_digest=row.image_digest,
        log_leaf_index=leaf_index,
    )
