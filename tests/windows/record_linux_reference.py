"""Turn the one JSON line printed by a Kit job command into the Linux reference artifacts the parity test compares against.

    <job command> | python tests/windows/record_linux_reference.py --out linux-artifacts

Input line: {"image": "<name@sha256:...>", "passed": true, "outputs": {"<posix relative path>": "<sha256>", ...}}
Writes ``linux-kit-manifest.json`` (the hashes) and ``linux-kit-report.json`` (image, passed). Files are written as bytes: LF on every OS.
Exits 1 when the job did not pass, printed nothing usable, or the image is not pinned by digest. WRITTEN, NOT RUN.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_PIN = re.compile(r"@sha256:[0-9a-f]{64}$")


def parse_job_output(text: str) -> dict:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("the job printed nothing")
    report = json.loads(lines[-1])
    if report.get("passed") is not True:
        raise ValueError("the job did not pass")
    if not _PIN.search(str(report.get("image", ""))):
        raise ValueError("the image is not pinned by digest")
    outputs = report.get("outputs")
    if not isinstance(outputs, dict) or not outputs or not all(re.fullmatch(r"[0-9a-f]{64}", str(v)) for v in outputs.values()):
        raise ValueError("outputs must be a non-empty {path: sha256} object")
    return report


def write_reference(report: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "linux-kit-manifest.json").write_bytes((json.dumps(dict(sorted(report["outputs"].items())), indent=2) + "\n").encode("utf-8"))
    (out / "linux-kit-report.json").write_bytes((json.dumps({"image": report["image"], "passed": True}, indent=2) + "\n").encode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("linux-artifacts"))
    a = ap.parse_args(argv)
    try:
        write_reference(parse_job_output(sys.stdin.read()), a.out)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"not recorded: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
