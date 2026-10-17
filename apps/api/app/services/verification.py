"""Output verification (Phase 4).

This performs EXECUTION / SHAPE verification of a job's declared output — NOT a
cryptographic proof of useful work. A container exiting 0 is never treated as
proof; we check the output conforms to the workload's expected shape.

* text-generation -> output has ``{"text": <str>}``.
* batch-inference  -> output has a manifest whose item count equals the number of
  input items, and every item carries a per-item ``status``.

Returns a JSON-serializable verification record stored on the job.
"""

from __future__ import annotations

from typing import Any

_NOTE = "execution/shape verification only; NOT a cryptographic proof of work"


def verify_output(
    workload_type: str, job_input: dict[str, Any], output: Any
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    valid = True
    reason: str | None = None

    if not isinstance(output, dict):
        return {
            "valid": False,
            "workload_type": workload_type,
            "reason": "output_not_object",
            "checks": {"output_is_object": False},
            "note": _NOTE,
        }

    if workload_type == "text-generation":
        has_text = isinstance(output.get("text"), str)
        checks["output_has_text_str"] = has_text
        valid = has_text
        if not has_text:
            reason = "missing_text_field"

    elif workload_type == "batch-inference":
        items = job_input.get("items")
        expected = len(items) if isinstance(items, list) else None
        # The Phase 4 contract's batch output uses `items`; also accept the
        # synonyms `manifest`/`results` for forward compatibility.
        records = None
        for key in ("items", "manifest", "results"):
            candidate = output.get(key)
            if isinstance(candidate, list):
                records = candidate
                break
        got = len(records) if isinstance(records, list) else None
        count_ok = expected is not None and got == expected
        per_item_ok = isinstance(records, list) and all(
            isinstance(r, dict) and "status" in r for r in records
        )
        checks["expected_items"] = expected
        checks["manifest_items"] = got
        checks["item_count_matches"] = count_ok
        checks["all_items_have_status"] = per_item_ok
        valid = bool(count_ok and per_item_ok)
        if not valid:
            reason = "manifest_mismatch"

    else:
        valid = False
        reason = "unknown_workload"
        checks["known_workload"] = False

    return {
        "valid": valid,
        "workload_type": workload_type,
        "reason": reason,
        "checks": checks,
        "note": _NOTE,
    }
