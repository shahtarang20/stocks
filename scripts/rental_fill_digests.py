#!/usr/bin/env python3
"""Record a built rental template image in the API's template list (apps/api/app/data/rental_templates.json).

    python scripts/rental_fill_digests.py --slug pytorch-jupyter --image ghcr.io/<owner>/rental-pytorch-jupyter@sha256:<64 hex>

Writes the full pinned image reference into the template's ``digest`` field (a template without one is hidden and refused). The reference
must be pinned by digest, on an allowed public registry, and the template must already exist in the file. Nothing else in the file changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "apps" / "api" / "app" / "data" / "rental_templates.json"
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}$")
ALLOWED_REGISTRIES = ("ghcr.io", "docker.io", "quay.io", "nvcr.io")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def registry_of(image: str) -> str:
    first = image.split("/", 1)[0]
    return first if ("." in first or ":" in first or first == "localhost") else "docker.io"


def fill(path: Path, slug: str, image: str) -> str:
    if not SLUG_RE.fullmatch(slug or ""):
        raise SystemExit(f"not a valid template name: {slug!r}")
    if not IMAGE_RE.fullmatch(image or ""):
        raise SystemExit("the image must look like registry/name@sha256:<64 hex characters> (pinned by digest, lower case)")
    if registry_of(image) not in ALLOWED_REGISTRIES:
        raise SystemExit(f"registry {registry_of(image)!r} is not one of {', '.join(ALLOWED_REGISTRIES)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path}: {type(exc).__name__}")
    items = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SystemExit("the template file has an unexpected shape")
    entry = next((t for t in items if isinstance(t, dict) and t.get("slug") == slug), None)
    if entry is None:
        raise SystemExit(f"template {slug!r} is not in {path.name}")
    previous = entry.get("digest")
    entry["digest"] = image
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return previous or ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--slug", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    a = ap.parse_args(argv)
    previous = fill(Path(a.file), a.slug, a.image)
    print(f"{a.slug}: digest set" + (f" (was {previous})" if previous else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
