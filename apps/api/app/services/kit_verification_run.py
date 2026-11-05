"""U6 wiring: reference re-runs for Kit jobs and the verdict, reusing the shadow-run machinery.

* ``maybe_schedule_kit_reference`` (called from the result path inside ``safe_hook``) samples a completed,
  billed Kit job whose manifest has a ``verification`` section and creates an unbilled reference run on a
  different provider via ``verification.create_shadow_job``. Rate: KIT_VERIFICATION_SAMPLE_RATE, which the
  manifest's ``sample_fraction`` can only lower.
* ``sweep_kit_verifications`` (worker step) judges finished pairs: downloads both jobs' output files from
  the data plane, hash-checks them, and calls ``record_kit_verification``. Absence of evidence (missing file,
  failed reference run, store outage) is ``inconclusive`` or retried, never a failure of the provider.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.base import utcnow
from app.models.data_plane import DataFile, DataObject
from app.models.jobs import Job
from app.models.kits import Kit
from app.models.verification import RedundancyCheck
from app.services import job_fsm as fsm
from app.services import verification as verification_service

logger = logging.getLogger("computecoin.kit_verification")


async def _manifest_of(db: Any, job: Job) -> dict | None:
    if not job.kit_id or not job.kit_version:
        return None
    kit = (await db.execute(select(Kit).where(Kit.slug == job.kit_id, Kit.version == job.kit_version))).scalar_one_or_none()
    return kit.manifest if kit is not None else None


def sample_rate(manifest: dict) -> float:
    base = float(settings.KIT_VERIFICATION_SAMPLE_RATE)
    frac = (manifest.get("verification") or {}).get("sample_fraction")
    if isinstance(frac, (int, float)) and not isinstance(frac, bool) and 0 <= frac <= 1:
        base = min(base, float(frac))
    return max(0.0, min(1.0, base))


async def maybe_schedule_kit_reference(db: Any, job: Job) -> Any:
    if job.workload_type != "kit" or verification_service.is_unbilled_job(job):
        return None
    if verification_service.is_service_replica_job(job):
        return None  # a Service replica has no declared outputs to compare (U11)
    manifest = await _manifest_of(db, job)
    if not manifest or not isinstance(manifest.get("verification"), dict):
        return None
    if not verification_service.should_shadow(sample_rate(manifest)):
        return None
    return await verification_service.create_shadow_job(db, job)


async def _output_files(db: Any, job_id: uuid.UUID, owner_id: uuid.UUID) -> list[tuple[DataFile, str]]:
    rows = (await db.execute(
        select(DataFile, DataObject.storage_key).join(DataObject, DataObject.id == DataFile.object_id).where(
            DataFile.owner_id == owner_id, DataFile.deleted_at.is_(None),
            DataFile.name.like(f"out/{job_id}/%"),
        ).order_by(DataFile.name)
    )).all()
    return [(r[0], r[1]) for r in rows]


def _download(url: str, dest: Path, sha256: str, limit: int) -> bool:
    import hashlib

    import httpx

    h = hashlib.sha256()
    n = 0
    with httpx.stream("GET", url, timeout=120.0) as r, open(dest, "wb") as fh:
        if r.status_code != 200:
            return False
        for chunk in r.iter_bytes(1 << 20):
            n += len(chunk)
            if n > limit:
                return False
            h.update(chunk)
            fh.write(chunk)
    return h.hexdigest() == sha256


async def _fetch_all(db: Any, files: list[tuple[DataFile, str]], dest: Path) -> list[Path] | None:
    from app.services.data_plane import require_enabled

    store = require_enabled()
    out: list[Path] = []
    for f, key in files:
        url = store.presign_download(key, 300, Path(f.name).name, f.content_type)
        target = dest / Path(f.name).name
        ok = await asyncio.to_thread(_download, url, target, f.sha256, int(settings.KIT_VERIFICATION_MAX_FILE_BYTES))
        if not ok:
            return None
        out.append(target)
    return out


async def judge_check(db: Any, check: RedundancyCheck) -> str:
    """Resolve one pending kit reference check. Returns the new status or 'pending' when it must wait."""
    primary = await db.get(Job, check.primary_job_id)
    shadow = await db.get(Job, check.shadow_job_id) if check.shadow_job_id else None
    if primary is None or shadow is None:
        return await _finish(db, check, "inconclusive", {"reason": "job_missing"})
    if shadow.status not in fsm.TERMINAL_STATES:
        return "pending"
    if shadow.status != fsm.COMPLETED:
        return await _finish(db, check, "inconclusive", {"reason": "reference_run_not_completed"})
    manifest = await _manifest_of(db, primary)
    if not manifest or not isinstance(manifest.get("verification"), dict):
        return await _finish(db, check, "inconclusive", {"reason": "no_verification_section"})
    if verification_service.is_service_replica_job(primary):
        return await _finish(db, check, "inconclusive", {"reason": "service_replica_has_no_outputs"})
    p_files = await _output_files(db, primary.id, primary.customer_id)
    r_files = await _output_files(db, shadow.id, shadow.customer_id)
    if not p_files or not r_files:
        return await _finish(db, check, "inconclusive", {"reason": "output_files_missing"})
    with tempfile.TemporaryDirectory(prefix="kitverify-") as tmp:
        pd, rd = Path(tmp) / "primary", Path(tmp) / "reference"
        pd.mkdir()
        rd.mkdir()
        try:
            pp = await _fetch_all(db, p_files, pd)
            rp = await _fetch_all(db, r_files, rd)
        except Exception:
            logger.exception("kit verification fetch failed; will retry")
            return "pending"
        if pp is None or rp is None:
            return await _finish(db, check, "inconclusive", {"reason": "output_download_or_hash_failed"})
        res = await verification_service.record_kit_verification(
            db, primary, manifest, pp, rp, reference_job_id=shadow.id
        )
    status = (res.detail or {}).get("status")
    new = "matched" if status == "passed" else "mismatched" if status == "failed" else "inconclusive"
    check.matched = True if new == "matched" else False if new == "mismatched" else None
    return await _finish(db, check, new, {"kit_verification_status": status})


async def _finish(db: Any, check: RedundancyCheck, status: str, detail: dict) -> str:
    check.status = status
    check.resolved_at = utcnow()
    check.detail = {**(check.detail or {}), **detail}
    await db.flush()
    return status


async def sweep_kit_verifications(sm: Any, limit: int = 20) -> dict[str, int]:
    """Judge pending Kit reference checks. Each check is isolated in its own session and try/except."""
    counts: dict[str, int] = {"checked": 0, "resolved": 0, "errors": 0}
    async with sm() as db:
        ids = (await db.execute(
            select(RedundancyCheck.id).join(Job, Job.id == RedundancyCheck.primary_job_id).where(
                RedundancyCheck.status.in_(("pending", "running")), Job.workload_type == "kit"
            ).order_by(RedundancyCheck.created_at).limit(limit)
        )).scalars().all()
    for cid in ids:
        counts["checked"] += 1
        try:
            async with sm() as db:
                check = await db.get(RedundancyCheck, cid)
                if check is not None and await judge_check(db, check) != "pending":
                    counts["resolved"] += 1
                await db.commit()
        except Exception:
            counts["errors"] += 1
            logger.exception("kit verification check %s failed (retried next sweep)", cid)
    return counts
