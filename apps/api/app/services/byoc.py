"""BYOC (bring your own container) validation, scanning gate and plan building (U8).

Design: docs/architecture/U8-byoc-design.md. Everything here fails closed: an empty registry allowlist, an
unconfigured scanner, a scanner error, a denylisted digest or a stale/missing scan all refuse the job.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.byoc import ByocImageDenylist, ByocImageScan
from app.schemas.byoc import ByocSpec
from app.services import image_scan

BASE_ENV_ALLOW = frozenset({"TZ", "LANG", "LC_ALL", "PYTHONUNBUFFERED", "OMP_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                            "MKL_NUM_THREADS"})
ENV_PREFIX_ALLOW = ("APP_", "CUSTOM_")
RESERVED_ENV_PREFIXES = ("COMPUTECOIN_", "LD_", "DOCKER_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUNTIME_OF_ISOLATION = {"gvisor": "runsc", "microvm": "kata-qemu"}
PIDS_LIMIT = 256


class ByocError(Exception):
    def __init__(self, code: str, status: int = 422, errors: list[dict] | None = None):
        super().__init__(code)
        self.code, self.http_status, self.errors = code, status, errors or [{"field": "spec", "code": code}]


def _err(field: str, code: str, message: str | None = None) -> dict:
    d = {"field": field, "code": code}
    if message:
        d["message"] = message
    return d


def _csv(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def registry_allowlist() -> list[str]:
    return [p.lower().rstrip("/") for p in _csv(settings.BYOC_REGISTRY_ALLOWLIST)]


def registry_allowed(registry_path: str) -> bool:
    """``registry_path`` is the image without the digest. Empty allowlist = nothing allowed."""
    ref = registry_path.lower()
    return any(ref == a or ref.startswith(a + "/") for a in registry_allowlist())


def env_name_allowed(name: str) -> bool:
    if any(name.startswith(p) for p in RESERVED_ENV_PREFIXES):
        return False
    if name in BASE_ENV_ALLOW or name in set(_csv(settings.BYOC_ENV_ALLOWLIST)):
        return True
    return any(name.startswith(p) for p in ENV_PREFIX_ALLOW)


def _pydantic_errors(exc: ValidationError) -> list[dict]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "spec"
        out.append(_err(loc, "unknown_field" if e["type"] == "extra_forbidden" else e["type"], str(e["msg"])[:300]))
    return out


def validate_byoc_spec(raw: Any) -> ByocSpec:
    """Shape and policy checks that need no database. Raises ByocError(422) with per-field errors."""
    if not isinstance(raw, dict):
        raise ByocError("invalid_spec", 422, [_err("spec", "must_be_object")])
    if not settings.BYOC_REGISTRY_ALLOWLIST.strip():
        raise ByocError("byoc_not_enabled", 403, [_err("image", "byoc_registry_allowlist_empty")])
    try:
        spec = ByocSpec.model_validate(raw)
    except ValidationError as exc:
        raise ByocError("invalid_spec", 422, _pydantic_errors(exc))
    errors: list[dict] = []
    if not registry_allowed(spec.registry_path):
        errors.append(_err("image", "registry_not_allowed", "the image registry is not on the allowlist"))
    for name in spec.env:
        if not env_name_allowed(name):
            errors.append(_err(f"env.{name}", "env_name_not_allowed"))
    if spec.network.egress and not settings.BYOC_ALLOW_EGRESS:
        errors.append(_err("network.egress", "egress_not_allowed", "this platform does not allow BYOC egress"))
    g, r = spec.resources.gpu, spec.resources
    if (g.count or 1) > settings.BYOC_MAX_GPUS:
        errors.append(_err("resources.gpu.count", "above_byoc_limit", f"at most {settings.BYOC_MAX_GPUS}"))
    if r.cpu is not None and r.cpu > settings.BYOC_MAX_CPUS:
        errors.append(_err("resources.cpu", "above_byoc_limit", f"at most {settings.BYOC_MAX_CPUS}"))
    if r.ram_gb is not None and r.ram_gb * 1024 > settings.BYOC_MAX_MEMORY_MB:
        errors.append(_err("resources.ram_gb", "above_byoc_limit", f"at most {settings.BYOC_MAX_MEMORY_MB // 1024}"))
    if spec.max_runtime_seconds is not None and spec.max_runtime_seconds > settings.BYOC_MAX_RUNTIME_SECONDS:
        errors.append(_err("max_runtime_seconds", "above_byoc_limit", f"at most {settings.BYOC_MAX_RUNTIME_SECONDS}"))
    names = [o.name for o in spec.outputs]
    if len(set(names)) != len(names):
        errors.append(_err("outputs", "duplicate_name"))
    in_names = [i.name for i in spec.inputs]
    if len(set(in_names)) != len(in_names):
        errors.append(_err("inputs", "duplicate_name"))
    if errors:
        raise ByocError("invalid_spec", 422, errors)
    return spec


def effective_resources(spec: ByocSpec) -> dict:
    """Billing is per GPU-second, so a BYOC job holds at least one GPU slot (default and minimum 1)."""
    g, r = spec.resources.gpu, spec.resources
    return {
        "gpu_count": int(g.count or 1), "min_vram_gb": g.min_vram_gb, "min_compute_capability": g.min_compute_capability,
        "cpu": r.cpu, "ram_gb": r.ram_gb, "disk_gb": r.disk_gb,
    }


def max_runtime_seconds(spec: ByocSpec) -> int:
    return int(min(spec.max_runtime_seconds or settings.JOB_MAX_RUNTIME_DEFAULT, settings.BYOC_MAX_RUNTIME_SECONDS))


def build_byoc_block(spec: ByocSpec) -> dict:
    """What rides to the agent (``v2.byoc`` on the claim). The agent re-validates every field."""
    res = effective_resources(spec)
    return {
        "image": spec.image,
        "isolation": spec.isolation,
        "runtime": RUNTIME_OF_ISOLATION[spec.isolation],
        "command": list(spec.command),
        "args": list(spec.args),
        "env": dict(sorted(spec.env.items())),
        "gpu_count": res["gpu_count"],
        "memory_mb": int(spec.resources.ram_gb * 1024) if spec.resources.ram_gb else min(2048, settings.BYOC_MAX_MEMORY_MB),
        "cpus": str(spec.resources.cpu or 1),
        "pids_limit": PIDS_LIMIT,
        "max_runtime_seconds": max_runtime_seconds(spec),
    }


# ---- denylist and scanning -----------------------------------------------------------------

async def is_denylisted(db: AsyncSession, digest: str) -> ByocImageDenylist | None:
    return (await db.execute(select(ByocImageDenylist).where(ByocImageDenylist.digest == digest))).scalar_one_or_none()


async def ensure_image_cleared(db: AsyncSession, spec_image: str, digest: str) -> None:
    """Submit-time gate: not denylisted, and a clean scan (cached within the TTL, else run now). Raises ByocError."""
    if await is_denylisted(db, digest) is not None:
        raise ByocError("byoc_image_denied", 422, [_err("image", "image_denied")])
    now = utcnow()
    row = (await db.execute(select(ByocImageScan).where(ByocImageScan.digest == digest))).scalar_one_or_none()
    ttl = dt.timedelta(seconds=max(0, int(settings.BYOC_SCAN_TTL_SECONDS)))
    if row is not None and row.status == image_scan.CLEAN and now - ensure_aware(row.scanned_at) <= ttl:
        return
    result = await image_scan.get_scanner().scan(spec_image)
    if result.status == image_scan.UNCONFIGURED:
        raise ByocError("byoc_scanner_unconfigured", 503, [_err("image", "scanner_unconfigured")])
    if result.status == image_scan.ERROR:
        raise ByocError("byoc_scan_failed", 503, [_err("image", "scan_failed")])
    stored = image_scan.CLEAN if result.status == image_scan.CLEAN else image_scan.DENIED
    if row is None:
        db.add(ByocImageScan(digest=digest, scanner=result.scanner, status=stored, findings={"findings": result.findings},
                             scanned_at=now))
    else:
        row.scanner, row.status, row.findings, row.scanned_at = result.scanner, stored, {"findings": result.findings}, now
    await db.flush()
    if stored != image_scan.CLEAN:
        raise ByocError("byoc_image_refused_by_scan", 422,
                        [_err("image", "image_refused_by_scan", "the image scan found problems"),
                         *[_err("image", f["code"]) for f in result.findings[:10] if isinstance(f, dict) and "code" in f]])


def job_byoc(job: Any) -> dict | None:
    spec = getattr(job, "spec_v2", None)
    block = spec.get("byoc") if isinstance(spec, dict) else None
    return block if isinstance(block, dict) else None


async def claim_denied_reason(db: AsyncSession, job: Any) -> str | None:
    """Claim-time re-check for a BYOC job; a reason code to fail the claimed job with, or None. Fails closed."""
    block = job_byoc(job)
    if block is None:
        return None
    image = str(block.get("image", ""))
    m = re.search(r"@(sha256:[0-9a-f]{64})$", image)
    if m is None or (job.spec_v2 or {}).get("kit_image") != image:
        return "byoc_integrity_failed"
    if await is_denylisted(db, m.group(1)) is not None:
        return "byoc_image_denied"
    row = (await db.execute(select(ByocImageScan).where(ByocImageScan.digest == m.group(1)))).scalar_one_or_none()
    if row is None or row.status != image_scan.CLEAN:
        return "byoc_image_not_cleared"
    return None


async def validate_byoc_job(db: AsyncSession, job: Any) -> None:
    """VALIDATING step: the image must still be cleared. Raises JobValidationError."""
    from app.services.scheduler import JobValidationError

    if not settings.BYOC_REGISTRY_ALLOWLIST.strip():
        raise JobValidationError("byoc_not_enabled")
    reason = await claim_denied_reason(db, job)
    if reason is not None:
        raise JobValidationError(reason)
