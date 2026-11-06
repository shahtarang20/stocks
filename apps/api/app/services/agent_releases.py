"""Agent release management service (W6): verify and store signed manifests, pause and roll back, choose a host's release.

The API never signs anything: the release engineer signs the manifest offline with the update key (Ed25519) and the API
verifies it with ``settings.AGENT_RELEASE_SIGNING_PUBLIC_KEY`` before storing. The format is the one the agent verifies
(provider-agent/computecoin_agent/updater/manifest.py); both sides must keep ``SIGN_PREFIX`` and the canonical encoding equal.

Rings run canary -> early -> general. A release carries the WIDEST ring it is offered to; a host in ring R is offered
releases whose ring is R or wider. The server assigns a host's ring (``agent_ring_assignments``, default ``general``);
the host's own report can only move it to a WIDER ring (fewer releases), never a narrower one.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.agent_releases import AgentRelease, AgentRingAssignment

SIGN_PREFIX = b"computecoin-agent-update-v1\n"
PRODUCT = "computecoin-agent"
RINGS = ("canary", "early", "general")
PLATFORMS = ("windows", "linux")
MAX_ARTIFACT_BYTES = 1024**3
_REQUIRED = {"schema", "product", "version", "platform", "artifact_url", "sha256", "size_bytes", "min_supported", "ring", "issued_at"}
_OPTIONAL = {"expires_at", "revoked_versions"}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SIG_RE = re.compile(r"^[0-9a-f]{128}$")
_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION_RE = re.compile(
    r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:-([0-9A-Za-z][0-9A-Za-z.\-]{0,31}))?(?:\+[0-9A-Za-z][0-9A-Za-z.\-]{0,31})?$"
)


class ReleaseError(Exception):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = status


# ---- versions ----------------------------------------------------------------------------------

def is_valid_version(value: Any) -> bool:
    return isinstance(value, str) and _VERSION_RE.match(value) is not None


def version_key(value: str) -> tuple:
    m = _VERSION_RE.match(value)
    if m is None:
        raise ReleaseError("invalid_version")
    major, minor, patch, pre = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if pre is None:
        return (major, minor, patch, 1, ())
    return (major, minor, patch, 0, tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")))


def ring_rank(ring: str) -> int:
    return RINGS.index(ring)


# ---- signing -----------------------------------------------------------------------------------

def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _public_key():
    raw = (settings.AGENT_RELEASE_SIGNING_PUBLIC_KEY or "").strip()
    if not _KEY_RE.match(raw):
        raise ReleaseError("agent_release_signing_key_not_configured", 503)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw)), hashlib.sha256(bytes.fromhex(raw)).hexdigest()[:16]


def verify_manifest(manifest: dict, signature_hex: str) -> str:
    """Signing key id, or ReleaseError. Fails closed on any problem."""
    key, key_id = _public_key()
    from cryptography.exceptions import InvalidSignature

    try:
        sig = bytes.fromhex(signature_hex)
        if len(sig) != 64 or not _SIG_RE.match(signature_hex):
            raise ValueError
        key.verify(sig, SIGN_PREFIX + canonical_bytes(manifest))
    except (ValueError, InvalidSignature):
        raise ReleaseError("agent_release_signature_invalid", 422)
    return key_id


def validate_manifest(m: Any) -> None:
    """Structure only (the signature is checked first, by the caller)."""
    if not isinstance(m, dict):
        raise ReleaseError("manifest_malformed")
    keys = set(m)
    if keys - _REQUIRED - _OPTIONAL or _REQUIRED - keys:
        raise ReleaseError("manifest_fields")
    if m["schema"] != 1 or m["product"] != PRODUCT:
        raise ReleaseError("manifest_schema")
    if not is_valid_version(m["version"]) or not is_valid_version(m["min_supported"]):
        raise ReleaseError("manifest_version")
    if version_key(m["min_supported"]) > version_key(m["version"]):
        raise ReleaseError("manifest_floor_above_version")
    if m["platform"] not in PLATFORMS or m["ring"] not in RINGS:
        raise ReleaseError("manifest_platform_or_ring")
    if not isinstance(m["sha256"], str) or not _SHA_RE.match(m["sha256"]):
        raise ReleaseError("manifest_sha256")
    size = m["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_BYTES:
        raise ReleaseError("manifest_size")
    url = urlsplit(m["artifact_url"]) if isinstance(m["artifact_url"], str) and len(m["artifact_url"]) <= 2048 else None
    if url is None or url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ReleaseError("manifest_artifact_url")
    issued = m["issued_at"]
    if isinstance(issued, bool) or not isinstance(issued, int) or issued < 0:
        raise ReleaseError("manifest_issued_at")
    exp = m.get("expires_at")
    if exp is not None and (isinstance(exp, bool) or not isinstance(exp, int) or exp <= issued):
        raise ReleaseError("manifest_expires_at")
    rev = m.get("revoked_versions", [])
    if not isinstance(rev, list) or len(rev) > 64 or not all(is_valid_version(v) for v in rev):
        raise ReleaseError("manifest_revoked_versions")


# ---- publish / state changes -------------------------------------------------------------------

async def publish_release(db: AsyncSession, *, manifest: dict, signature: str, published_by: uuid.UUID | None) -> AgentRelease:
    key_id = verify_manifest(manifest, signature)  # signature FIRST: nothing in an unsigned manifest is trusted
    validate_manifest(manifest)
    row = AgentRelease(
        platform=manifest["platform"],
        version=manifest["version"],
        ring=manifest["ring"],
        min_supported=manifest["min_supported"],
        artifact_sha256=manifest["sha256"],
        manifest=manifest,
        manifest_sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
        signature=signature,
        signing_key_id=key_id,
        status="published",
        published_by=published_by,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise ReleaseError("agent_release_exists", 409)
    return row


async def get_release(db: AsyncSession, release_id: uuid.UUID) -> AgentRelease:
    row = await db.get(AgentRelease, release_id)
    if row is None:
        raise ReleaseError("agent_release_not_found", 404)
    return row


async def set_status(db: AsyncSession, release_id: uuid.UUID, status: str, *, by: uuid.UUID | None, reason: str | None) -> AgentRelease:
    """published <-> paused; rolled_back is final (a rolled-back release is never served again; publish a fixed one)."""
    row = await get_release(db, release_id)
    if row.status == "rolled_back":
        raise ReleaseError("agent_release_rolled_back", 409)
    if status not in ("published", "paused", "rolled_back"):
        raise ReleaseError("invalid_status")
    row.status = status
    row.status_reason = (reason or "")[:255] or None
    row.status_changed_at = utcnow()
    row.status_changed_by = by
    await db.flush()
    return row


async def rollback_release(
    db: AsyncSession, release_id: uuid.UUID, *, by: uuid.UUID | None, reason: str | None,
    replacement: Optional[dict] = None,
) -> tuple[AgentRelease, Optional[AgentRelease]]:
    """Stop distributing a release for good. Hosts already running it can only move to a NEWER release, because an agent
    refuses a downgrade unless the manifest it gets lists its running version in ``revoked_versions``. So an optional
    ``replacement`` ({manifest, signature}) is published in the same step, and must revoke the rolled-back version."""
    row = await get_release(db, release_id)
    if row.status == "rolled_back":
        raise ReleaseError("agent_release_rolled_back", 409)
    new_row: Optional[AgentRelease] = None
    if replacement is not None:
        m, sig = replacement.get("manifest"), replacement.get("signature")
        if not isinstance(m, dict) or not isinstance(sig, str):
            raise ReleaseError("replacement_malformed")
        if m.get("platform") != row.platform:
            raise ReleaseError("replacement_platform_mismatch")
        if row.version not in (m.get("revoked_versions") or []):
            raise ReleaseError("replacement_must_revoke_rolled_back_version")
        if is_valid_version(m.get("version")) and version_key(m["version"]) <= version_key(row.version):
            # a replacement the agents would treat as a downgrade or no change would not move anyone
            raise ReleaseError("replacement_must_be_newer")
        new_row = await publish_release(db, manifest=m, signature=sig, published_by=by)
    row.status = "rolled_back"
    row.status_reason = (reason or "")[:255] or None
    row.status_changed_at = utcnow()
    row.status_changed_by = by
    await db.flush()
    return row, new_row


# ---- host side ---------------------------------------------------------------------------------

async def assigned_ring(db: AsyncSession, machine_id: uuid.UUID) -> str:
    row = await db.get(AgentRingAssignment, machine_id)
    return row.ring if row is not None and row.ring in RINGS else "general"


async def assign_ring(db: AsyncSession, machine_id: uuid.UUID, ring: str, *, by: uuid.UUID | None) -> AgentRingAssignment:
    if ring not in RINGS:
        raise ReleaseError("invalid_ring")
    row = await db.get(AgentRingAssignment, machine_id)
    if row is None:
        row = AgentRingAssignment(machine_id=machine_id, ring=ring, updated_by=by)
        db.add(row)
    else:
        row.ring, row.updated_by, row.updated_at = ring, by, utcnow()
    await db.flush()
    return row


def effective_ring(assigned: str, reported: Optional[str]) -> str:
    """The server decides. The host may only choose a WIDER (later) ring than the one it was assigned."""
    if reported in RINGS and ring_rank(reported) > ring_rank(assigned):
        return reported
    return assigned


async def select_release(
    db: AsyncSession, *, platform: str, host_ring: str, current_version: str
) -> Optional[AgentRelease]:
    """The release this host should run, or None when it has nothing to do.

    Candidates: published, same platform, ring at least as wide as the host's. The highest version wins. A release that is
    not newer than the host is offered only when it authorises the downgrade (lists the host's version as revoked)."""
    if platform not in PLATFORMS or host_ring not in RINGS or not is_valid_version(current_version):
        raise ReleaseError("invalid_request")
    rows = (await db.execute(
        select(AgentRelease).where(AgentRelease.platform == platform, AgentRelease.status == "published")
    )).scalars().all()
    cur = version_key(current_version)
    best: Optional[AgentRelease] = None
    for r in rows:
        if ring_rank(r.ring) < ring_rank(host_ring):
            continue
        key = version_key(r.version)
        if key == cur:
            continue
        if key < cur and current_version not in ((r.manifest or {}).get("revoked_versions") or []):
            continue
        if best is None or key > version_key(best.version) or (
            key == version_key(best.version) and ring_rank(r.ring) > ring_rank(best.ring)
        ):
            best = r
    return best


def served_manifest(row: AgentRelease) -> dict:
    """Flat signed manifest exactly as stored: the payload plus its signature."""
    return {**row.manifest, "signature": row.signature}
