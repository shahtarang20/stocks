"""Shared runtime helpers for ComputeCoin Kit entrypoints (container contract, U3).

Contract (agent runtime v2, docs/architecture/U3-agent-runtime-v2-design.md):
  COMPUTECOIN_INPUT          /out/input.json  job input; Kit params are `{"params": {...}}` (a flat object is also accepted)
  COMPUTECOIN_OUTPUT         /out/output.json result summary written by the entrypoint
  COMPUTECOIN_INPUTS_DIR     /in     read-only, one file per declared input name (/in/<name>)
  COMPUTECOIN_CACHE_DIR      /cache  read-only Kit assets (wave-1 Kits bake weights into the image instead)
  COMPUTECOIN_CHECKPOINT_DIR writable checkpoint dir; COMPUTECOIN_RESUME=1 when restoring
  COMPUTECOIN_JOB_ID
Outputs go under /out at the paths declared in kit.json. No network at run time.

Checkpoint convention (COMMIT marker): write files into the checkpoint dir, THEN atomically replace
`COMMIT` with a JSON manifest {"seq": n, "files": {name: sha256}}. The agent snapshots only when `COMMIT`
exists. On resume a Kit trusts ONLY files named in COMMIT whose SHA-256 still matches.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from typing import Any

INPUT = os.environ.get("COMPUTECOIN_INPUT", "/out/input.json")
OUTPUT = os.environ.get("COMPUTECOIN_OUTPUT", "/out/output.json")
INPUTS_DIR = os.environ.get("COMPUTECOIN_INPUTS_DIR", "/in")
CACHE_DIR = os.environ.get("COMPUTECOIN_CACHE_DIR", "/cache")
CKPT_DIR = os.environ.get("COMPUTECOIN_CHECKPOINT_DIR", "/ckpt")
OUT_DIR = os.environ.get("COMPUTECOIN_OUT_DIR", "/out")
RESUME = os.environ.get("COMPUTECOIN_RESUME") == "1"


class KitError(Exception):
    """A failure with a stable machine code; the entrypoint exits non-zero after recording it."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_manifest(root: str, manifest_path: str) -> str:
    """Check every baked asset against the SHA-256 recorded at build time. Returns the manifest digest."""
    raw = open(manifest_path, "rb").read()
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        if name.startswith("/") or ".." in name.split("/"):
            raise KitError("bad_asset_manifest", name)
        if sha256_file(os.path.join(root, name)) != digest:
            raise KitError("asset_hash_mismatch", name)
    return hashlib.sha256(raw).hexdigest()


# ---- params ---------------------------------------------------------------------------------

_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def _type_ok(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def validate(value: Any, schema: dict, path: str = "params") -> Any:
    """Small JSON-Schema subset (type, enum, min/max, length, pattern, items, required, defaults,
    additionalProperties=false). The platform already validated against the full schema (U1); this
    is a defensive second check so the container never trusts its input blindly."""
    t = schema.get("type")
    if t and not _type_ok(value, t):
        raise KitError("invalid_param", f"{path}: expected {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise KitError("invalid_param", f"{path}: not one of {schema['enum']}")
    if isinstance(value, float) and not math.isfinite(value):
        # json.load accepts NaN / Infinity; NaN compares False with every bound below and would slip past minimum/maximum.
        raise KitError("invalid_param", f"{path}: not a finite number")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise KitError("invalid_param", f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise KitError("invalid_param", f"{path}: above maximum")
        if "multipleOf" in schema and value % schema["multipleOf"] != 0:
            raise KitError("invalid_param", f"{path}: not a multiple of {schema['multipleOf']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise KitError("invalid_param", f"{path}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise KitError("invalid_param", f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise KitError("invalid_param", f"{path}: pattern mismatch")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise KitError("invalid_param", f"{path}: too many items")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise KitError("invalid_param", f"{path}: too few items")
        if "items" in schema:
            for i, v in enumerate(value):
                validate(v, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise KitError("invalid_param", f"{path}: unknown field {sorted(extra)[0]}")
        out = dict(value)
        for k, sub in props.items():
            if k in out:
                out[k] = validate(out[k], sub, f"{path}.{k}")
            elif "default" in sub:
                out[k] = sub["default"]
        for k in schema.get("required", []):
            if k not in out:
                raise KitError("invalid_param", f"{path}: missing {k}")
        return out
    return value


def load_params(schema_path: str) -> dict:
    with open(INPUT, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise KitError("invalid_input", "input.json must be an object")
    params = data.get("params", data)
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return validate(params, schema)


def input_file(name: str, required: bool = True) -> str | None:
    path = os.path.join(INPUTS_DIR, name)
    if os.path.isfile(path):
        return path
    if required:
        raise KitError("missing_input", name)
    return None


# ---- checkpoints ----------------------------------------------------------------------------

def commit(files: list[str], seq: int, root: str | None = None) -> None:
    """Publish a consistent checkpoint: files must already be fully written inside `root`."""
    root = root or CKPT_DIR
    listing = {os.path.relpath(f, root): sha256_file(f) for f in files}
    tmp = os.path.join(root, "COMMIT.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"seq": seq, "files": listing}, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(root, "COMMIT"))


def committed_files(root: str | None = None) -> dict[str, str]:
    """On resume: {relative name: path} of checkpoint files named in COMMIT whose hash still matches."""
    root = root or CKPT_DIR
    if not RESUME:
        return {}
    try:
        with open(os.path.join(root, "COMMIT"), "r", encoding="utf-8") as fh:
            listing = json.load(fh).get("files", {})
    except (OSError, ValueError):
        return {}
    ok: dict[str, str] = {}
    for rel, digest in listing.items():
        p = os.path.realpath(os.path.join(root, rel))
        if p.startswith(os.path.realpath(root) + os.sep) and os.path.isfile(p) and sha256_file(p) == digest:
            ok[rel] = p
    return ok


# ---- results --------------------------------------------------------------------------------

def write_result(payload: dict) -> None:
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
    os.replace(tmp, OUTPUT)


def run_main(fn) -> None:
    try:
        write_result({"ok": True, **fn()})
    except KitError as exc:
        write_result({"ok": False, "error": {"code": exc.code, "message": str(exc)[:500]}})
        print(f"kit error {exc.code}: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # unexpected: still leave a result the platform can show
        write_result({"ok": False, "error": {"code": "internal_error", "message": type(exc).__name__}})
        raise
