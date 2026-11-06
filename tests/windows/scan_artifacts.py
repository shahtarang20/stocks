"""Fail when a text file in an artifact folder contains something that looks like a secret (L7). Run before every upload:

    python tests/windows/scan_artifacts.py e2e-artifacts

It reuses the support bundle's scrubber (``computecoin_agent.support_bundle.Redactor``): a file is clean only if scrubbing it changes
nothing. Binary files are skipped, and reported by name so a reviewer knows they were not scanned. WRITTEN, NOT RUN.
"""

from __future__ import annotations

import sys
from pathlib import Path


def scan(folder: Path) -> tuple[list[str], list[str]]:
    from computecoin_agent.support_bundle import Redactor

    dirty: list[str] = []
    skipped: list[str] = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(p.relative_to(folder).as_posix())
            continue
        probe = Redactor()
        if probe.text(text) != text:
            dirty.append(f"{p.relative_to(folder).as_posix()} ({', '.join(sorted(probe.counts))})")
    return dirty, skipped


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    folder = Path(args[0]) if args else Path("e2e-artifacts")
    if not folder.is_dir():
        print(f"no such folder: {folder}", file=sys.stderr)
        return 2
    dirty, skipped = scan(folder)
    for name in skipped:
        print(f"not scanned (binary): {name}")
    for name in dirty:
        print(f"LOOKS LIKE A SECRET: {name}", file=sys.stderr)
    return 1 if dirty else 0


if __name__ == "__main__":
    sys.exit(main())
