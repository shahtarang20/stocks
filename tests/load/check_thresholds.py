#!/usr/bin/env python3
"""Fail (exit 1) when a Locust run regresses past the agreed thresholds (B4.4).

Usage: check_thresholds.py <locust_csv_prefix> [thresholds.json]

Reads <prefix>_stats.csv (written by `locust --csv <prefix>`). Thresholds are NOT
invented here: thresholds.json is committed with null values until the owner agrees
numbers from a real baseline run (docs/operations/load-test-baseline.md). A null
threshold is reported as "not set" and never fails; an all-null file therefore only
checks that the run produced data and had no failures when max_error_rate is set.

thresholds.json shape (per-request-name overrides optional):
  {"default": {"p95_ms": null, "p99_ms": null, "max_error_rate": null, "min_rps": null},
   "per_name": {"POST /v1/jobs": {"p95_ms": 800}}}
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    prefix = argv[1]
    th_path = Path(argv[2]) if len(argv) > 2 else Path(__file__).with_name("thresholds.json")
    stats_path = Path(f"{prefix}_stats.csv")
    if not stats_path.exists():
        print(f"missing {stats_path}")
        return 2
    th = json.loads(th_path.read_text())
    default = th.get("default", {})
    per_name = th.get("per_name", {})

    failures: list[str] = []
    unset: set[str] = set()
    rows = list(csv.DictReader(stats_path.open()))
    if not rows:
        print("no rows in stats csv")
        return 2

    for row in rows:
        name = row["Name"]
        reqs = int(row["Request Count"])
        if reqs == 0:
            continue
        fails = int(row["Failure Count"])
        err = fails / reqs
        p95 = float(row["95%"])
        p99 = float(row["99%"])
        rps = float(row["Requests/s"])
        limits = {**default, **per_name.get(name, {})}
        # The "Aggregated" row is checked for error rate and throughput only.
        checks = [("max_error_rate", err, "max"), ("min_rps", rps, "min")]
        if name != "Aggregated":
            checks += [("p95_ms", p95, "max"), ("p99_ms", p99, "max")]
        for key, value, kind in checks:
            limit = limits.get(key)
            if limit is None:
                unset.add(key)
                continue
            bad = value > limit if kind == "max" else value < limit
            if bad:
                failures.append(f"{name}: {key} {value:.4g} vs limit {limit}")
        print(f"{name}: n={reqs} err={err:.2%} p95={p95:.0f}ms p99={p99:.0f}ms rps={rps:.2f}")

    if unset:
        print(f"note: thresholds not set (never fail): {', '.join(sorted(unset))}")
    if failures:
        print("\nREGRESSION:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nwithin thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
