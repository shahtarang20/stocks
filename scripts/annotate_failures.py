"""Turn a pytest report into GitHub annotations.

GitHub's job logs need a login to read, but annotations are shown on the run page and through the public API, so a failure
is easy to find and read without one. Usage: python scripts/annotate_failures.py <pytest output file> [title]

Prints ``::error`` lines (one for the failed test names, one for the end of the report). Never fails: it only reports.
"""

from __future__ import annotations

import re
import sys


def escape(text: str) -> str:
    """GitHub's workflow-command escaping for the message part."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def build(report: str, title: str = "Tests failed") -> list[str]:
    failed = re.findall(r"^(?:FAILED|ERROR) (.+)$", report, re.M)
    lines = []
    if failed:
        names = "\n".join(failed[:40]) + (f"\n... and {len(failed) - 40} more" if len(failed) > 40 else "")
        lines.append(f"::error title={title}: {len(failed)} failing::{escape(names)}")
    tail = report[-4000:]
    lines.append(f"::error title={title}: end of the report::{escape(tail)}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: annotate_failures.py <pytest output file> [title]", file=sys.stderr)
        return 0
    try:
        with open(argv[1], encoding="utf-8", errors="replace") as fh:
            report = fh.read()
    except OSError as exc:
        print(f"::warning::could not read the report: {type(exc).__name__}")
        return 0
    for line in build(report, argv[2] if len(argv) > 2 else "Tests failed"):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
