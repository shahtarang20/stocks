"""Artifact upload/download behind signed HMAC grants (Phase 4).

These endpoints use capability tokens, NOT user JWTs:

* ``PUT /v1/artifacts/upload/{token}`` — the agent uploads bytes with the upload
  grant returned by the claim. The token is scoped to a ``job_id`` + expiry. Size
  and content-type are enforced; the sha256 is recorded. The API ONLY stores the
  bytes — it never executes them.
* ``GET /v1/artifacts/download/{token}`` — serves an artifact's bytes to a holder
  of a valid download grant. Owners are the only ones who can obtain such a grant
  (issued by the owner-scoped job endpoint), so non-owners get 404 there.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.core.config import settings
from app.db.session import get_db
from app.models.jobs import Artifact, Job
from app.schemas.jobs import ArtifactUploadResult
from app.services.storage import (
    get_store,
    sha256_hex,
    verify_download_grant,
    verify_upload_grant,
)

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

_INVALID_GRANT = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_or_expired_grant"
)
_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")


@router.put("/upload/{token}", response_model=ArtifactUploadResult, status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    token: str,
    request: Request,
    content_type: str = Header(default="application/octet-stream", alias="Content-Type"),
    x_artifact_kind: str = Header(default="output", alias="X-Artifact-Kind"),
    db: AsyncSession = Depends(get_db),
) -> ArtifactUploadResult:
    job_id_str = verify_upload_grant(token)
    if job_id_str is None:
        raise _INVALID_GRANT
    try:
        job_id = uuid.UUID(job_id_str)
    except ValueError:
        raise _INVALID_GRANT

    job = await db.get(Job, job_id)
    if job is None:
        raise _NOT_FOUND

    # Normalize content type (strip any charset parameter) and enforce the allowlist.
    base_ct = content_type.split(";")[0].strip().lower()
    if base_ct not in settings.artifact_content_types:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="content_type_not_allowed"
        )

    data = await request.body()
    if len(data) > settings.ARTIFACT_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="artifact_too_large"
        )

    artifact_id = uuid.uuid4()
    object_key = f"artifacts/{job_id}/{artifact_id}"
    digest = sha256_hex(data)
    get_store().put(object_key, data, base_ct)

    artifact = Artifact(
        id=artifact_id,
        job_id=job_id,
        kind=x_artifact_kind or "output",
        object_key=object_key,
        content_type=base_ct,
        size_bytes=len(data),
        sha256=digest,
    )
    db.add(artifact)
    await db.flush()

    return ArtifactUploadResult(
        artifact_id=artifact.id,
        sha256=digest,
        size=len(data),
        kind=artifact.kind,
        content_type=base_ct,
    )


@router.get("/download/{token}")
async def download_artifact(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    artifact_id_str = verify_download_grant(token)
    if artifact_id_str is None:
        raise _INVALID_GRANT
    try:
        artifact_id = uuid.UUID(artifact_id_str)
    except ValueError:
        raise _INVALID_GRANT

    artifact = await db.get(Artifact, artifact_id)
    if artifact is None:
        raise _NOT_FOUND
    try:
        data = get_store().get(artifact.object_key)
    except (FileNotFoundError, OSError):
        raise _NOT_FOUND
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={
            "X-Artifact-SHA256": artifact.sha256,
            # Prevent content sniffing / inline rendering of stored bytes (stored-XSS guard).
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{artifact_id}"',
        },
    )
