#!/usr/bin/env python3
"""Validate a BYOC job spec JSON locally (U8) with the SERVER's schema, and optionally lint a Dockerfile.

    python scripts/check_byoc_image_spec.py spec.json [--dockerfile Dockerfile] [--registry host/org ...]

Imports apps/api/app/schemas/byoc.py (strict pydantic model, unknown fields refused), so it needs pydantic installed
(the API environment). The server additionally enforces the registry allowlist setting, platform resource caps, the image
scan and the denylist; none of that is known here. `--registry` only checks the image prefix you expect. Exit 0 = shape OK.
Accepts the spec itself or an object `{"spec": {...}}` (the POST /v1/jobs/byoc body).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

# Keep identical to app/services/byoc.py (env_name_allowed); that module needs the database stack, so it is not imported.
BASE_ENV_ALLOW = frozenset({"TZ", "LANG", "LC_ALL", "PYTHONUNBUFFERED", "OMP_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                            "MKL_NUM_THREADS"})
ENV_PREFIX_ALLOW = ("APP_", "CUSTOM_")
RESERVED_ENV_PREFIXES = ("COMPUTECOIN_", "LD_", "DOCKER_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")


def env_name_problem(name: str) -> str | None:
    if any(name.startswith(p) for p in RESERVED_ENV_PREFIXES):
        return f"env {name}: reserved prefix"
    if name in BASE_ENV_ALLOW or name.startswith(ENV_PREFIX_ALLOW):
        return None
    return f"env {name}: not on the default allowlist (the platform may allow it via BYOC_ENV_ALLOWLIST)"


def check_spec(raw: object, registries: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors would be refused by the server's schema."""
    from pydantic import ValidationError

    from app.schemas.byoc import ByocSpec

    if isinstance(raw, dict) and "spec" in raw and "mode" not in raw:
        raw = raw["spec"]
    if not isinstance(raw, dict):
        return ["spec must be a JSON object"], []
    errors: list[str] = []
    warnings: list[str] = []
    try:
        spec = ByocSpec.model_validate(raw)
    except ValidationError as exc:
        for e in exc.errors():
            loc = ".".join(str(p) for p in e["loc"]) or "spec"
            errors.append(f"{loc}: {e['msg']}")
        return errors, warnings
    for name in spec.env:
        problem = env_name_problem(name)
        if problem:
            warnings.append(problem)
    if registries:
        ref = spec.registry_path.lower()
        if not any(ref == r.lower().rstrip("/") or ref.startswith(r.lower().rstrip("/") + "/") for r in registries):
            errors.append(f"image: {spec.registry_path} is not under any of --registry {registries}")
    if spec.network.egress:
        warnings.append("network.egress is refused unless the platform sets BYOC_ALLOW_EGRESS=true")
    if spec.resources.gpu.count is None:
        warnings.append("resources.gpu.count not set: BYOC holds at least 1 GPU (billing is per GPU-second)")
    if spec.max_runtime_seconds is None:
        warnings.append("max_runtime_seconds not set: the platform default cap applies")
    return errors, warnings


def lint_dockerfile(text: str) -> list[str]:
    """Heuristic warnings only; a Dockerfile cannot prove what the built image does."""
    out: list[str] = []
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M | re.I)
    if not froms:
        out.append("Dockerfile: no FROM line")
    for ref in froms:
        if "@sha256:" not in ref and not ref.startswith("${") and ref.lower() != "scratch":
            out.append(f"Dockerfile: base image {ref} is not pinned by digest")
        if ref.endswith(":latest"):
            out.append("Dockerfile: do not use :latest")
    users = re.findall(r"^USER\s+(\S+)", text, re.M | re.I)
    if not users or users[-1] in ("root", "0", "0:0"):
        out.append("Dockerfile: the final USER must be non-root")
    if re.search(r"^\s*ADD\s+https?://", text, re.M | re.I):
        out.append("Dockerfile: ADD of a remote URL; download with a hash check instead")
    ep = re.findall(r"^(?:ENTRYPOINT|CMD)\s+(.*)$", text, re.M | re.I)
    if ep and any(not line.lstrip().startswith("[") for line in ep):
        out.append("Dockerfile: use exec-form ENTRYPOINT/CMD (a JSON array), not a shell string")
    if re.search(r"^\s*VOLUME\b", text, re.M | re.I):
        out.append("Dockerfile: VOLUME has no effect on a read-only rootfs; write to /out")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", help="path to the spec JSON")
    ap.add_argument("--dockerfile", help="also lint this Dockerfile (warnings)")
    ap.add_argument("--registry", action="append", default=[], help="expected registry prefix, repeatable")
    a = ap.parse_args(argv)
    try:
        raw = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR cannot read spec: {exc}", file=sys.stderr)
        return 2
    errors, warnings = check_spec(raw, a.registry or None)
    if a.dockerfile:
        try:
            warnings += lint_dockerfile(Path(a.dockerfile).read_text(encoding="utf-8"))
        except OSError as exc:
            errors.append(f"cannot read Dockerfile: {exc}")
    for w in warnings:
        print("WARN  " + w, file=sys.stderr)
    for e in errors:
        print("ERROR " + e, file=sys.stderr)
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
