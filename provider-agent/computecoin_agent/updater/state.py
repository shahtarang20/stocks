"""Durable updater state (survives the process being replaced by the update itself).

One small JSON file, replaced atomically (write a sibling temp file, then ``os.replace``). A corrupt or unreadable file is
treated as EMPTY state: the worst outcome is a repeated attempt, never a skipped safety check, because every decision is
re-derived from the signed manifest.

Keys: ``known_good`` {version, sha256, path} | None, ``in_flight`` {from, to, sha256, path, started_at} | None,
``failed`` {version: {at, count, code}}, ``floor`` the highest ``min_supported`` ever seen in a verified manifest.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional


def _empty() -> dict:
    return {"known_good": None, "in_flight": None, "failed": {}, "floor": None}


class UpdateState:
    def __init__(self, path: "str | os.PathLike[str]") -> None:
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return _empty()
            out = _empty()
            for key in out:
                if key in raw:
                    out[key] = raw[key]
            if not isinstance(out["failed"], dict):
                out["failed"] = {}
            return out
        except (OSError, ValueError):
            return _empty()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- helpers ---- #
    @property
    def known_good(self) -> Optional[dict]:
        return self.data["known_good"]

    @property
    def in_flight(self) -> Optional[dict]:
        return self.data["in_flight"]

    def record_failure(self, version: str, code: str, now: float) -> None:
        prev = self.data["failed"].get(version) or {}
        self.data["failed"][version] = {"at": now, "count": int(prev.get("count", 0)) + 1, "code": code}
        self.save()

    def failed_recently(self, version: str, now: float, cooldown: float) -> bool:
        rec = self.data["failed"].get(version)
        return bool(rec) and now - float(rec.get("at", 0)) < cooldown

    def raise_floor(self, floor: str) -> None:
        from .version import compare

        current: Optional[str] = self.data["floor"]
        if current is None or compare(floor, current) > 0:
            self.data["floor"] = floor
            self.save()


def seed_known_good(
    state: UpdateState,
    *,
    version: str,
    artifact: "str | os.PathLike[str]",
    cache_dir: "str | os.PathLike[str]",
    force: bool = False,
) -> bool:
    """Record the installer that is being installed NOW as the rollback source, so the very first update can roll back.

    Called at install time (the MSI custom action and ``service install``). The artifact is copied into ``cache_dir`` (the
    updater's own ``<work>/cache``, same ``<version>-<sha16>.pkg`` naming as ``core.AgentUpdater._cache``), hashed, and
    written as ``known_good``. An existing known-good whose file is still there is NEVER replaced by a seed (it was proven by
    a health check; a seed was not) unless ``force``. Returns True when the state changed. Raises ``ValueError`` on bad
    input (invalid version, missing or oversized file) and ``OSError`` on copy errors; the callers treat both as "not
    seeded" and carry on, because a missing seed only means the first update cannot roll back (the updater then blocks
    claims rather than guess).
    """
    from .manifest import MAX_ARTIFACT_BYTES, sha256_file
    from .version import is_valid_version

    if not is_valid_version(version):
        raise ValueError("invalid version for the known-good seed")
    src = Path(artifact)
    if not src.is_file():
        raise ValueError("installer artifact not found")
    size = src.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise ValueError("installer artifact has an unusable size")
    existing = state.known_good
    if existing and not force and existing.get("path") and Path(str(existing["path"])).is_file():
        return False
    digest = sha256_file(src)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{version}-{digest[:16]}.pkg"
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if sha256_file(dest) != digest:  # the cached copy is what a rollback would install: it must be what was seeded
        try:
            dest.unlink()
        except OSError:
            pass
        raise ValueError("the cached copy does not match the installer")
    state.data["known_good"] = {"version": version, "sha256": digest, "path": str(dest)}
    state.save()
    return True
