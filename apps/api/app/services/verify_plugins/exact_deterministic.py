"""Exact-hash verification that applies only when the job declared ``deterministic=true`` (scientific kit).

The kit writes ``determinism.json`` (``{"deterministic": true|false}``) next to its outputs. If the independent REFERENCE
run says it was not deterministic (or the marker is missing or unreadable), nothing can be concluded and the outcome is
``inconclusive``: absence of evidence is never evidence of cheating. The primary's marker is not trusted; it is hashed like
every other file, so a host that flips it fails the exact comparison. When the reference is deterministic the comparison
is the plain byte-for-byte :class:`ExactHashPlugin`. Written, never run.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import INCONCLUSIVE, VerifyOutcome
from .exact import ExactHashPlugin

MARKER = "determinism.json"
MAX_MARKER_BYTES = 4096


class ExactDeterministicPlugin:
    method = "exact_deterministic"
    default_thresholds: dict[str, float] = {}
    threshold_bounds: dict[str, tuple[float, float]] = {}

    def verify(self, primary, reference, thresholds=None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE)
        marker = next((Path(p) for p in reference if Path(p).name == MARKER), None)
        if marker is None:
            out.reason = "reference_missing" if not reference else "determinism_marker_missing"
            return out
        try:
            if marker.stat().st_size > MAX_MARKER_BYTES:
                out.reason = "determinism_marker_unreadable"
                return out
            flag = json.loads(marker.read_text(encoding="utf-8")).get("deterministic")
        except (OSError, ValueError, AttributeError):
            out.reason = "determinism_marker_unreadable"
            return out
        if flag is not True:
            out.reason = "kit_not_deterministic"
            return out
        inner = ExactHashPlugin().verify(primary, reference, thresholds)
        inner.method = self.method
        return inner
