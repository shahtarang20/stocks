"""Register built workload images per GPU runtime and log them as IMAGE_HASH (P3.4)."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_registry import ModelVersion, ModelVersionImage
from app.services.transparency_log import append_transparency_log_entry

GPU_RUNTIMES = ("cuda", "rocm")
_DIGEST_RE = re.compile(r"^(?:[a-z0-9][a-z0-9._/:-]*)?@sha256:[0-9a-f]{64}$")


class ImageRegistrationError(Exception):
    def __init__(self, code: str, http_status: int = 400):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


async def register_image(
    db: AsyncSession,
    *,
    version_tag: str,
    gpu_runtime: str,
    image_digest: str,
    weights_manifest_sha256: str | None = None,
) -> tuple[ModelVersionImage, int]:
    """Upsert the image row for (model version, runtime) and append an IMAGE_HASH leaf.

    Only digest-pinned references are accepted (``name@sha256:<64 hex>``). Re-registering a
    different digest replaces the row and logs a new leaf, so the history stays in the log.
    Returns (row, leaf_index).
    """
    runtime = (gpu_runtime or "").lower().strip()
    if runtime not in GPU_RUNTIMES:
        raise ImageRegistrationError("invalid_gpu_runtime")
    if not _DIGEST_RE.fullmatch(image_digest or ""):
        raise ImageRegistrationError("image_not_pinned_by_digest")
    mv = (
        await db.execute(select(ModelVersion).where(ModelVersion.version_tag == version_tag))
    ).scalar_one_or_none()
    if mv is None:
        raise ImageRegistrationError("model_version_not_found", 404)
    if mv.runtime == "simulated":
        raise ImageRegistrationError("model_version_is_simulated", 409)

    row = (
        await db.execute(
            select(ModelVersionImage).where(
                ModelVersionImage.model_version_id == mv.id,
                ModelVersionImage.gpu_runtime == runtime,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ModelVersionImage(model_version_id=mv.id, gpu_runtime=runtime, image_digest=image_digest)
        db.add(row)
    else:
        row.image_digest = image_digest
    if runtime == "cuda":
        mv.image_digest = image_digest  # D4: the legacy column is the CUDA build
    await db.flush()
    leaf = await append_transparency_log_entry(
        db,
        "IMAGE_HASH",
        {
            "model_version": version_tag,
            "gpu_runtime": runtime,
            "image_digest": image_digest,
            "weights_manifest_sha256": weights_manifest_sha256,
        },
    )
    return row, int(leaf.leaf_index)
