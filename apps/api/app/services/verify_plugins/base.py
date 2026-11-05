"""Plug-in contract for kit verification methods (U6, ADR-0006 section 12).

A plug-in compares what the host produced (``primary``) with a reference produced by an independent
re-run of a sample (``reference``). It never raises for "the provider's output is bad": that is a
``failed`` outcome. It reports ``inconclusive`` when WE cannot judge (missing tool, unreadable
reference, invalid thresholds): absence of evidence is never evidence of cheating, and an
inconclusive outcome must not slash or withhold pay on its own.

Provider-supplied files are untrusted input: plug-ins cap file sizes and pixel counts and never
shell out with a shell.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

PASSED = "passed"
FAILED = "failed"
INCONCLUSIVE = "inconclusive"

NOTE = "kit verification: sampled re-run comparison; thresholds are an owner decision"


@dataclass
class VerifyOutcome:
    method: str
    status: str  # passed | failed | inconclusive
    reason: str | None = None
    scores: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    def to_detail(self) -> dict[str, Any]:
        """JSON-safe record stored in ``verification_results.detail`` and shown on the job page."""
        return {
            "method": self.method,
            "status": self.status,
            "reason": self.reason,
            "scores": self.scores,
            "thresholds": self.thresholds,
            "checks": self.checks,
            "note": NOTE,
        }


class VerifyPlugin(Protocol):
    method: str
    #: Conservative defaults. OWNER DECISION: acceptance thresholds per kit are not set by engineering.
    default_thresholds: dict[str, float]
    #: (low, high) bounds a manifest may set; anything outside is refused as invalid_thresholds.
    threshold_bounds: dict[str, tuple[float, float]]

    def verify(self, primary: list[Path], reference: list[Path], thresholds: dict[str, float]) -> VerifyOutcome: ...


class ThresholdError(ValueError):
    pass


def resolve_thresholds(
    defaults: dict[str, float], bounds: dict[str, tuple[float, float]], override: Any
) -> dict[str, float]:
    """Merge manifest thresholds over defaults. Unknown keys, non-numbers and out-of-range values are refused."""
    merged = dict(defaults)
    if override is None:
        return merged
    if not isinstance(override, dict):
        raise ThresholdError("thresholds_must_be_object")
    for key, value in override.items():
        if key not in defaults:
            raise ThresholdError(f"unknown_threshold:{key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ThresholdError(f"threshold_not_a_number:{key}")
        low, high = bounds[key]
        if not (low <= float(value) <= high) or value != value:
            raise ThresholdError(f"threshold_out_of_range:{key}")
        merged[key] = float(value)
    return merged


def choose_sample_indices(total: int, fraction: float, min_items: int = 1, max_items: int = 16) -> list[int]:
    """Pick which items to re-run. Uses the OS CSPRNG so a provider cannot predict the sample."""
    if total <= 0:
        return []
    count = max(min_items, int(round(total * max(0.0, min(1.0, fraction)))))
    count = min(total, count, max_items)
    rng = secrets.SystemRandom()
    return sorted(rng.sample(range(total), count))


def pair_by_name(primary: list[Path], reference: list[Path]) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Pair files by basename. Returns (pairs, names present on only one side)."""
    p = {x.name: x for x in primary}
    r = {x.name: x for x in reference}
    pairs = [(p[n], r[n]) for n in sorted(p.keys() & r.keys())]
    unmatched = sorted((p.keys() ^ r.keys()))
    return pairs, unmatched
