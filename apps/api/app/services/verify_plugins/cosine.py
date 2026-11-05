"""Cosine similarity between the host's embedding matrix and a reference re-run (batch-embeddings).

Files per side: ``embeddings.npy`` (2-D float matrix, rows in input order) and optionally ``ids.json`` (ordered ids).
Rows are compared in order: every row's cosine similarity must be >= ``min_cosine``. The id lists must be identical
and in the same order, and the shapes equal. Non-finite values in the host's matrix fail; in the reference they are
``inconclusive``. Written, never run. numpy API to verify against current docs (L4): ``numpy.load(mmap_mode, allow_pickle)``.
Threshold is an OWNER DECISION.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import FAILED, INCONCLUSIVE, PASSED, ThresholdError, VerifyOutcome, resolve_thresholds

MAX_FILE_BYTES = 4 * 1024**3
MAX_IDS_BYTES = 64 * 1024**2  # json.loads of a list of small items costs ~10x its size in memory
CHUNK_ROWS = 4096
MATRIX = "embeddings.npy"
IDS = "ids.json"


def row_cosines(a, b):
    """Cosine similarity per row of two equal-shape 2-D float arrays. Two zero rows count as 1.0, one zero row as 0.0."""
    import numpy as np

    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    na, nb = np.linalg.norm(a, axis=1), np.linalg.norm(b, axis=1)
    dot = np.einsum("ij,ij->i", a, b)
    denom = na * nb
    sim = np.where(denom > 0, dot / np.where(denom > 0, denom, 1.0), 0.0)
    sim = np.where((na == 0) & (nb == 0), 1.0, sim)
    return np.clip(sim, -1.0, 1.0)


def _by_name(files: list[Path], name: str) -> Path | None:
    return next((f for f in files if f.name == name), None)


class CosinePlugin:
    method = "cosine"
    default_thresholds: dict[str, float] = {"min_cosine": 0.99}  # OWNER DECISION, per vector
    threshold_bounds: dict[str, tuple[float, float]] = {"min_cosine": (0.5, 1.0)}

    def verify(self, primary, reference, thresholds=None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE)
        try:
            th = resolve_thresholds(self.default_thresholds, self.threshold_bounds, thresholds or None)
        except ThresholdError as exc:
            out.reason = f"invalid_thresholds:{exc}"
            return out
        out.thresholds = th
        try:
            import numpy as np
        except ImportError:
            out.reason = "numpy_unavailable"
            return out
        rp, pp = _by_name(list(reference), MATRIX), _by_name(list(primary), MATRIX)
        if rp is None:
            out.reason = "reference_missing"
            return out
        if pp is None:
            out.status, out.reason = FAILED, "primary_output_missing"
            return out
        try:
            if rp.stat().st_size > MAX_FILE_BYTES:
                out.reason = "file_too_large"
                return out
            b = np.load(rp, mmap_mode="r", allow_pickle=False)
        except Exception:  # numpy raises ValueError, TypeError, OSError, EOFError... for a malformed .npy
            out.reason = "unreadable_embeddings"
            return out
        # The PRIMARY is host-controlled: a file that is too large or cannot be read is the host's bad output (failed),
        # never "inconclusive" (that would let a host dodge verification with a corrupt file).
        try:
            if pp.stat().st_size > MAX_FILE_BYTES:
                out.status, out.reason = FAILED, "primary_file_too_large"
                return out
            a = np.load(pp, mmap_mode="r", allow_pickle=False)
        except Exception:
            out.status, out.reason = FAILED, "primary_unreadable_embeddings"
            return out
        if b.ndim != 2 or b.dtype.kind != "f":
            out.reason = "reference_not_a_float_matrix"
            return out
        if a.ndim != 2 or a.dtype.kind != "f" or a.shape != b.shape:
            out.status, out.reason = FAILED, "shape_mismatch"
            out.checks.append({"check": "shape", "ok": False, "primary": list(a.shape), "reference": list(b.shape)})
            return out
        # ids: ordered, identical when both sides provide them; one-sided ids are a mismatch.
        ip, ir = _by_name(list(primary), IDS), _by_name(list(reference), IDS)
        if (ip is None) != (ir is None):
            out.status, out.reason = FAILED, "ids_file_set_mismatch"
            return out
        if ip is not None and ir is not None:
            try:
                if ir.stat().st_size > MAX_IDS_BYTES:
                    out.reason = "file_too_large"
                    return out
                ref_ids = json.loads(ir.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                out.reason = "unreadable_ids"
                return out
            try:
                if ip.stat().st_size > MAX_IDS_BYTES:
                    out.status, out.reason = FAILED, "primary_ids_too_large"
                    return out
                same = json.loads(ip.read_text(encoding="utf-8")) == ref_ids
            except (OSError, ValueError, RecursionError):  # host-controlled file (deeply nested JSON raises RecursionError)
                out.status, out.reason = FAILED, "primary_unreadable_ids"
                return out
            out.checks.append({"check": "ids_equal_in_order", "ok": same})
            if not same:
                out.status, out.reason = FAILED, "ids_mismatch"
                return out
        if a.shape[0] == 0:
            out.reason = "reference_empty"
            return out
        lowest, total, bad_rows, first_bad = 1.0, 0.0, 0, None
        for i in range(0, a.shape[0], CHUNK_ROWS):
            ca, cb = np.asarray(a[i:i + CHUNK_ROWS]), np.asarray(b[i:i + CHUNK_ROWS])
            if not np.isfinite(cb).all():
                out.reason = "reference_not_finite"
                return out
            if not np.isfinite(ca).all():
                out.status, out.reason = FAILED, "primary_not_finite"
                return out
            sims = row_cosines(ca, cb)
            lowest = min(lowest, float(sims.min()))
            total += float(sims.sum())
            below = np.nonzero(sims < th["min_cosine"])[0]
            if below.size:
                bad_rows += int(below.size)
                first_bad = int(i + below[0]) if first_bad is None else first_bad
        out.scores = {"rows": int(a.shape[0]), "min_cosine": round(lowest, 8),
                      "mean_cosine": round(total / a.shape[0], 8), "rows_below": bad_rows, "first_bad_row": first_bad}
        ok = bad_rows == 0
        out.checks.append({"check": "every_row_cosine_at_least_threshold", "ok": ok})
        out.status = PASSED if ok else FAILED
        out.reason = None if ok else "cosine_below_threshold"
        return out
