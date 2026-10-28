"""GPU runtime compatibility (AMD GPU support, D1/D4/D5): the one place these rules live.

A machine offers one GPU runtime: ``cuda`` (NVIDIA) or ``rocm`` (AMD). A GPU job asks for
``cuda`` (the default), ``rocm`` or ``any``. Every placement path (scheduler, failover,
shadow re-runs) and the claim's image choice use these helpers, so a CUDA job never lands on
an AMD machine, a machine is never sent an image built for the other runtime, and an AMD
machine is never asked for an isolation tier it cannot provide (D5: container only in v1).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_registry import ModelVersion, ModelVersionImage
from app.models.provider import HardwareInventory

RUNTIMES = ("cuda", "rocm")
# D5: tiers an AMD GPU job may use in v1. gVisor / microVM / privacy tiers are refused, never downgraded.
ROCM_TIERS = ("container",)


def machine_gpu_runtime(machine: Any) -> str | None:
    """The runtime a machine's GPUs use, or None for a CPU-only machine.

    GPU machines enrolled before AMD support have no runtime recorded: they are NVIDIA (D8)."""
    hw = getattr(machine, "hardware", None)
    if hw is None or not (hw.gpu_count or 0):
        return None
    return (hw.gpu_runtime or "cuda").lower().strip()


async def gpu_runtime_of_machine(db: AsyncSession, machine_id: Any) -> str | None:
    """Like machine_gpu_runtime, by query: safe where the machine's hardware row is not loaded
    (an async session cannot lazy-load it)."""
    if machine_id is None:
        return None
    row = (
        await db.execute(
            select(HardwareInventory.gpu_count, HardwareInventory.gpu_runtime).where(HardwareInventory.machine_id == machine_id)
        )
    ).first()
    if row is None or not (row.gpu_count or 0):
        return None
    return (row.gpu_runtime or "cuda").lower().strip()


def job_gpu_runtime(job: Any) -> str:
    return (getattr(job, "required_gpu_runtime", None) or "cuda").lower().strip()


def is_gpu_compatible(
    job: Any = None,
    machine: Any = None,
    image_runtimes: set[str] | None = None,
    *,
    job_gpu_runtime: str | None = None,
    job_gpu_count: int | None = None,
    isolation_tier: str | None = None,
) -> tuple[bool, str | None]:
    """Decide job/machine GPU compatibility (D3, D4, D5): the canonical helper.

    Used by validate_job, _eligible_machines and inference routing.

    Returns (True, None) if compatible; (False, reason) if not.
    Reasons:
      - 'machine_not_found': target machine does not exist.
      - 'gpus': machine has fewer GPUs than requested by the job.
      - 'no_gpu': machine has no GPUs for a GPU job.
      - 'gpu_runtime': runtime mismatch (e.g. cuda job on rocm machine, or rocm job on cuda machine).
      - 'amd_isolation': AMD GPU job requested with non-container tier (D5).
      - 'no_image': a hardware job's model has no image for the machine's GPU runtime (D4).

    ``image_runtimes`` is the set of runtimes the job's model has an image for
    (``model_image_runtimes``); pass it for hardware jobs, None for simulated ones. It is looked
    up by the caller because this check is synchronous and an async session cannot lazy-load.
    """
    if machine is None:
        return False, "machine_not_found"

    req_count = job_gpu_count if job_gpu_count is not None else (getattr(job, "gpu_count", 0) or 0)

    if req_count > 0:
        hw = getattr(machine, "hardware", None)
        machine_gpus = hw.gpu_count if hw else 0
        if machine_gpus < req_count:
            return False, "gpus"

        machine_rt = (hw.gpu_runtime or "cuda").lower().strip() if (hw and hw.gpu_count > 0) else None
        if machine_rt is None:
            return False, "no_gpu"

        machine_vendor = (hw.gpu_vendor or "nvidia").lower().strip() if (hw and hw.gpu_count > 0) else "nvidia"

        # D3: One vendor per job: required_gpu_runtime: cuda | rocm | any. Default cuda.
        req_rt = (
            job_gpu_runtime
            if job_gpu_runtime is not None
            else (getattr(job, "required_gpu_runtime", None) or "cuda")
        ).lower().strip()

        if req_rt != "any" and req_rt != machine_rt:
            return False, "gpu_runtime"

        # D5: Isolation for AMD GPU jobs is container only in v1.
        if machine_vendor == "amd" or machine_rt == "rocm":
            tier = (
                isolation_tier
                if isolation_tier is not None
                else (getattr(job, "isolation_tier", None) or "container")
            ).lower().strip()
            if tier not in ("container", "open"):
                return False, "amd_isolation"
    else:
        hw = getattr(machine, "hardware", None)
        machine_rt = (hw.gpu_runtime or "cuda").lower().strip() if (hw and hw.gpu_count > 0) else "cuda"

    # D4: a hardware GPU job needs an image built for this machine's runtime.
    if req_count > 0 and image_runtimes is not None and machine_rt not in image_runtimes:
        return False, "no_image"

    return True, None


def runtime_mismatch_reason(job: Any, machine: Any, image_runtimes: set[str] | None = None) -> str | None:
    """Why ``machine`` cannot run ``job``'s GPU work, or None when it can. Delegates to is_gpu_compatible."""
    ok, reason = is_gpu_compatible(job, machine, image_runtimes)
    return None if ok else reason


async def model_image_runtimes(db: AsyncSession, model_version: ModelVersion | None) -> set[str]:
    """Runtimes this model version has an image for: its per-runtime rows, plus ``cuda`` when the
    legacy ``image_digest`` is set (D4: that digest is the CUDA build)."""
    if model_version is None:
        return set()
    rows = (
        await db.execute(
            select(ModelVersionImage.gpu_runtime).where(ModelVersionImage.model_version_id == model_version.id)
        )
    ).scalars().all()
    runtimes = {str(r).lower().strip() for r in rows}
    if model_version.image_digest:
        runtimes.add("cuda")
    return runtimes


async def image_for_runtime(db: AsyncSession, model_version: ModelVersion | None, runtime: str | None) -> str | None:
    """The pinned image for this model version on ``runtime`` (queried, never lazy-loaded).

    ``cuda`` falls back to the legacy ``model_versions.image_digest`` (D4); ``rocm`` has no
    fallback: an image built for CUDA does not run on an AMD GPU. CPU-only work (runtime None)
    uses the legacy digest."""
    if model_version is None:
        return None
    if runtime is None:
        return model_version.image_digest
    row = (
        await db.execute(
            select(ModelVersionImage.image_digest).where(
                ModelVersionImage.model_version_id == model_version.id,
                ModelVersionImage.gpu_runtime == runtime,
            )
        )
    ).scalar_one_or_none()
    if row:
        return row
    return model_version.image_digest if runtime == "cuda" else None
