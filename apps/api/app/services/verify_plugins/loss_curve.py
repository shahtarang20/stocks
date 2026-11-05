"""Final training loss within a relative tolerance of an independent re-run (llm-finetune-lora).

Compares ``metrics.json`` from both sides (the Kit writes ``final_loss``, ``first_loss``, ``steps``, ``finite``).
Training is not bit-reproducible across GPU models or counts, so only the final loss is judged:
``|primary - reference| / max(|reference|, 1e-8) <= max_rel_diff``. A host reporting a non-finite or missing loss, or
``finite: false``, fails. A reference without a usable loss is ``inconclusive``. Adapter weights are NOT compared.
Written, never run. Threshold is an OWNER DECISION.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .base import FAILED, INCONCLUSIVE, PASSED, ThresholdError, VerifyOutcome, resolve_thresholds

MAX_FILE_BYTES = 8 * 1024**2
METRICS = "metrics.json"


def rel_diff(primary: float, reference: float) -> float:
    return abs(primary - reference) / max(abs(reference), 1e-8)


def _loss(data) -> float | None:
    if not isinstance(data, dict):
        return None
    v = data.get("final_loss")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        return None
    return float(v)


def _read(files: list[Path]):
    f = next((x for x in files if x.name == METRICS), None)
    if f is None:
        return "missing", None
    if f.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file_too_large")
    return "ok", json.loads(f.read_text(encoding="utf-8"))


class LossCurvePlugin:
    method = "loss_curve"
    default_thresholds: dict[str, float] = {"max_rel_diff": 0.25}  # OWNER DECISION
    threshold_bounds: dict[str, tuple[float, float]] = {"max_rel_diff": (0.0, 1.0)}

    def verify(self, primary, reference, thresholds=None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE)
        try:
            th = resolve_thresholds(self.default_thresholds, self.threshold_bounds, thresholds or None)
        except ThresholdError as exc:
            out.reason = f"invalid_thresholds:{exc}"
            return out
        out.thresholds = th
        try:
            rstate, rdata = _read(list(reference))
        except (OSError, ValueError, RecursionError):
            out.reason = "unreadable_metrics"
            return out
        try:
            pstate, pdata = _read(list(primary))
        except (OSError, ValueError, RecursionError):  # host-controlled file: its being unreadable is the host's failure
            out.status, out.reason = FAILED, "primary_metrics_unreadable"
            return out
        if rstate == "missing":
            out.reason = "reference_missing"
            return out
        rloss = _loss(rdata)
        if rloss is None or (isinstance(rdata, dict) and rdata.get("finite") is False):
            out.reason = "reference_loss_unusable"
            return out
        if pstate == "missing":
            out.status, out.reason = FAILED, "primary_output_missing"
            return out
        ploss = _loss(pdata)
        if ploss is None or (isinstance(pdata, dict) and pdata.get("finite") is False):
            out.status, out.reason = FAILED, "primary_loss_not_finite"
            return out
        d = rel_diff(ploss, rloss)
        out.scores = {"primary_final_loss": ploss, "reference_final_loss": rloss, "rel_diff": round(d, 8)}
        ok = d <= th["max_rel_diff"]
        out.checks.append({"check": "final_loss_within_relative_tolerance", "ok": ok})
        out.status = PASSED if ok else FAILED
        out.reason = None if ok else "final_loss_outside_tolerance"
        return out
