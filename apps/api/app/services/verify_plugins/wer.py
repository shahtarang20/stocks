"""Word error rate between a host's transcript and an independent reference transcript (whisper-transcribe).

``primary`` / ``reference`` hold transcript files (``transcript.txt|srt|vtt|json``, paired by basename). Text is
lower-cased, punctuation removed, split on whitespace; WER = word edit distance / reference word count. Passes when
WER <= ``max_wer`` for every pair. Unreadable or oversized transcripts are ``inconclusive`` (our problem), a missing
primary or a higher WER is ``failed``. Written, never run. Threshold is an OWNER DECISION.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .base import FAILED, INCONCLUSIVE, PASSED, ThresholdError, VerifyOutcome, pair_by_name, resolve_thresholds

MAX_FILE_BYTES = 8 * 1024**2
MAX_WORDS = 6000  # sampled windows are short; the edit distance is O(n*m)
SUFFIXES = {".txt", ".srt", ".vtt", ".json"}
_TS = re.compile(r"^\s*(\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->.*$")
_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)


class _Unreadable(Exception):
    pass


def transcript_text(path: Path) -> str | None:
    """Plain text of a transcript file, or None when it is a JSON file that is not a transcript."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise _Unreadable("file_too_large")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
        segs = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(segs, list):
            return None
        return " ".join(str(s.get("text", "")) for s in segs if isinstance(s, dict))
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.isdigit() or _TS.match(s):
            continue
        lines.append(s)
    return " ".join(lines)


def words(text: str) -> list[str]:
    return _PUNCT.sub(" ", text.lower()).split()


def edit_distance(a: list[str], b: list[str]) -> int:
    """Word-level Levenshtein distance, two rows."""
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1]


def wer(primary_words: list[str], reference_words: list[str]) -> float:
    """Edits to turn the reference into the primary, over the reference length (caller handles an empty reference)."""
    return edit_distance(reference_words, primary_words) / len(reference_words)


def _load(files: list[Path]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in files:
        if f.suffix.lower() not in SUFFIXES:
            continue
        text = transcript_text(f)
        if text is not None:
            out[f.name] = words(text)
    return out


class WerPlugin:
    method = "wer"
    default_thresholds: dict[str, float] = {"max_wer": 0.10}  # OWNER DECISION
    threshold_bounds: dict[str, tuple[float, float]] = {"max_wer": (0.0, 1.0)}

    def verify(self, primary, reference, thresholds=None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE)
        try:
            th = resolve_thresholds(self.default_thresholds, self.threshold_bounds, thresholds or None)
        except ThresholdError as exc:
            out.reason = f"invalid_thresholds:{exc}"
            return out
        out.thresholds = th
        try:
            ref = _load(list(reference))
        except (OSError, ValueError, RecursionError, _Unreadable):
            out.reason = "unreadable_transcript"
            return out
        try:
            prim = _load(list(primary))
        except (OSError, ValueError, RecursionError, _Unreadable):  # host-controlled files: unreadable is the host's failure
            out.status, out.reason = FAILED, "primary_transcript_unreadable"
            return out
        if not ref:
            out.reason = "reference_missing"
            return out
        if not prim:
            out.status, out.reason = FAILED, "primary_output_missing"
            return out
        if set(prim) != set(ref):
            out.status, out.reason = FAILED, "file_set_mismatch"
            out.checks.append({"check": "file_set", "ok": False, "unmatched": sorted(set(prim) ^ set(ref))[:20]})
            return out
        worst, per_file = 0.0, {}
        for name in sorted(ref):
            r, p = ref[name], prim[name]
            if len(r) > MAX_WORDS or len(p) > MAX_WORDS:
                out.reason = "transcript_too_long"
                return out
            if not r:
                if p:  # the reference heard nothing: we cannot judge a host that heard something
                    out.reason = "reference_empty"
                    return out
                per_file[name] = 0.0
                continue
            per_file[name] = round(wer(p, r), 6)
            worst = max(worst, per_file[name])
        out.scores = {"files": len(per_file), "max_wer": worst, "per_file": per_file}
        ok = worst <= th["max_wer"]
        out.checks.append({"check": "wer_at_most_threshold", "ok": ok})
        out.status = PASSED if ok else FAILED
        out.reason = None if ok else "wer_above_threshold"
        return out
