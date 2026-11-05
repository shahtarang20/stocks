"""Exact-hash verification for fully deterministic kits (byte-for-byte equal re-run)."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from .base import FAILED, INCONCLUSIVE, PASSED, VerifyOutcome, pair_by_name, resolve_thresholds, ThresholdError

MAX_FILE_BYTES = 8 * 1024**3


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class ExactHashPlugin:
    method = "exact"
    default_thresholds: dict[str, float] = {}
    threshold_bounds: dict[str, tuple[float, float]] = {}

    def verify(self, primary, reference, thresholds=None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE, thresholds={})
        try:
            resolve_thresholds(self.default_thresholds, self.threshold_bounds, thresholds or None)
        except ThresholdError as exc:
            out.reason = f"invalid_thresholds:{exc}"
            return out
        if not reference:
            out.reason = "reference_missing"
            return out
        if not primary:
            out.status, out.reason = FAILED, "primary_output_missing"
            return out
        pairs, unmatched = pair_by_name(primary, reference)
        if unmatched:
            out.status, out.reason = FAILED, "file_set_mismatch"
            out.checks.append({"check": "file_set", "ok": False, "unmatched": unmatched[:20]})
            return out
        bad: list[str] = []
        for a, b in pairs:
            try:
                if a.stat().st_size > MAX_FILE_BYTES or b.stat().st_size > MAX_FILE_BYTES:
                    out.reason = "file_too_large"
                    return out
                same = secrets.compare_digest(sha256_file(a), sha256_file(b))
            except OSError:
                out.reason = "unreadable_file"
                return out
            if not same:
                bad.append(a.name)
        out.scores = {"files": len(pairs), "mismatched": len(bad)}
        out.checks.append({"check": "sha256_equal", "ok": not bad, "mismatched": bad[:20]})
        out.status = PASSED if not bad else FAILED
        out.reason = None if not bad else "hash_mismatch"
        return out
