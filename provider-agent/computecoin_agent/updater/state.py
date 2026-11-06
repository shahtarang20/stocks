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
