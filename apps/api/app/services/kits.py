"""Kit registry service (U1): signed manifests, publishing, and Job Spec v2 validation.

Signature scheme: Ed25519 over ``SIGN_PREFIX + canonical_manifest_bytes``. The public key comes from
``settings.KIT_SIGNING_PUBLIC_KEY``; no private key exists in this code base (L7).
Library calls to verify against current docs before merge (L4): cryptography Ed25519PublicKey.from_public_bytes /
verify, jsonschema Draft202012Validator.check_schema / iter_errors.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.kits import Kit
from app.schemas.job_spec_v2 import NOT_SUPPORTED_YET, JobSpecV2

SIGN_PREFIX = b"computecoin-kit-v1\n"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PARAM_SCHEMA_BYTES = 64 * 1024
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:]{0,200}@sha256:[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-+]{0,30}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CATEGORIES = {"generative", "3d", "video", "enhancement", "inference", "training", "audio", "science", "other"}
SUPPORTED_MODES = {"batch", "service", "session"}
# 'batch' kits run as ordinary jobs (POST /v1/jobs/v2 and fan-outs); 'service' kits only as U11 service replicas
# (POST /v1/services); 'session' kits only as U10 interactive sessions (POST /v1/sessions).
BATCH_MODES = frozenset({"batch"})
SERVICE_MODES = frozenset({"service"})


class KitError(Exception):
    def __init__(self, code: str, status: int = 422, errors: list[dict] | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = status
        self.errors = errors or [{"field": "manifest", "code": code, "message": code}]


def _err(field: str, code: str, message: str | None = None) -> dict:
    return {"field": field, "code": code, "message": message or code}


# ---- signing -------------------------------------------------------------------------------

def canonical_manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _public_key():
    raw = (settings.KIT_SIGNING_PUBLIC_KEY or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        raise KitError("kit_signing_key_not_configured", 503)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw)), hashlib.sha256(bytes.fromhex(raw)).hexdigest()[:16]


def verify_manifest_signature(manifest: dict, signature_hex: str) -> str:
    """Return the signing key id, or raise KitError. Fails closed on any problem."""
    key, key_id = _public_key()
    from cryptography.exceptions import InvalidSignature

    try:
        sig = bytes.fromhex(signature_hex)
        if len(sig) != 64:
            raise ValueError
        key.verify(sig, SIGN_PREFIX + canonical_manifest_bytes(manifest))
    except (ValueError, InvalidSignature):
        raise KitError("kit_signature_invalid", 422, [_err("signature", "kit_signature_invalid")])
    return key_id


# ---- manifest validation -------------------------------------------------------------------

def _check_refs(node: Any, path: str, errors: list[dict]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("$ref", "$dynamicRef") and not (isinstance(v, str) and v.startswith("#")):
                errors.append(_err(path, "schema_remote_ref_refused", "only local '#/...' references are allowed"))
            _check_refs(v, f"{path}.{k}", errors)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_refs(v, f"{path}[{i}]", errors)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ASSETS = 32


def _validate_runtime_sections(manifest: dict) -> list[dict]:
    """Optional ``egress`` (B5 host:port allowlist), ``assets`` (hash-pinned read-only files) and ``checkpoint``."""
    from app.services.network_policy import NetworkPolicyError, validate_egress_entry

    errors: list[dict] = []
    eg = manifest.get("egress")
    if eg is not None:
        if not isinstance(eg, list) or len(eg) > 16:
            errors.append(_err("manifest.egress", "must_be_list_of_at_most_16"))
        else:
            for i, entry in enumerate(eg):
                try:
                    validate_egress_entry(entry)
                except NetworkPolicyError as exc:
                    errors.append(_err(f"manifest.egress[{i}]", "invalid_egress_entry", str(exc)[:200]))
    assets = manifest.get("assets")
    if assets is not None:
        if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
            errors.append(_err("manifest.assets", f"must_be_list_of_at_most_{MAX_ASSETS}"))
        else:
            seen: set[str] = set()
            for i, a in enumerate(assets):
                f = f"manifest.assets[{i}]"
                if not isinstance(a, dict):
                    errors.append(_err(f, "must_be_object"))
                    continue
                for k in a:
                    if k not in {"name", "sha256", "size_bytes", "url", "file_id", "owner_id", "cacheable"}:
                        errors.append(_err(f"{f}.{k}", "unknown_field"))
                if not isinstance(a.get("name"), str) or not NAME_RE.match(a["name"]):
                    errors.append(_err(f"{f}.name", "invalid_name"))
                elif a["name"] in seen:
                    errors.append(_err(f"{f}.name", "duplicate_name"))
                else:
                    seen.add(a["name"])
                if not isinstance(a.get("sha256"), str) or not SHA256_RE.match(a["sha256"]):
                    errors.append(_err(f"{f}.sha256", "invalid_sha256"))
                sz = a.get("size_bytes")
                if not isinstance(sz, int) or isinstance(sz, bool) or sz < 1:
                    errors.append(_err(f"{f}.size_bytes", "must_be_positive_integer"))
                if "cacheable" in a and not isinstance(a["cacheable"], bool):
                    errors.append(_err(f"{f}.cacheable", "must_be_boolean"))
                url, fid, oid = a.get("url"), a.get("file_id"), a.get("owner_id")
                if (url is None) == (fid is None):
                    errors.append(_err(f, "exactly_one_of_url_or_file_id"))
                if url is not None and (not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048):
                    errors.append(_err(f"{f}.url", "must_be_https_url"))
                if fid is not None:
                    try:
                        uuid.UUID(str(fid))
                        uuid.UUID(str(oid))
                    except (ValueError, TypeError):
                        errors.append(_err(f, "file_id_and_owner_id_must_be_uuids"))
    ck = manifest.get("checkpoint")
    if ck is not None:
        if not isinstance(ck, dict) or set(ck) - {"dir", "interval_seconds"}:
            errors.append(_err("manifest.checkpoint", "must_be_object_with_dir_and_interval_seconds"))
        else:
            d, iv = ck.get("dir"), ck.get("interval_seconds")
            if (not isinstance(d, str) or not d.startswith("/") or len(d) > 200 or ".." in d.split("/")
                    or "\x00" in d or d.rstrip("/") == "" or d.startswith(("/out", "/in", "/cache", "/proc", "/sys", "/dev"))):
                errors.append(_err("manifest.checkpoint.dir", "invalid_checkpoint_dir"))
            if not isinstance(iv, int) or isinstance(iv, bool) or not 1 <= iv <= 86400:
                errors.append(_err("manifest.checkpoint.interval_seconds", "must_be_integer_1_to_86400"))
    return errors


SESSION_MODES = ("terminal", "tcp")
SESSION_MAX_ARGS = 32
SESSION_MAX_ARG_LEN = 1024
SESSION_IDLE_MIN, SESSION_IDLE_MAX = 60, 24 * 3600


def validate_session_block(block: Any, field: str = "manifest.session") -> list[dict]:
    """U10 kit session block: ``{"mode": "terminal"|"tcp", "tcp_port"?: 1024-65535, "command"?: [str, ...],
    "idle_timeout_seconds"?: 60..86400}``. Strict: unknown keys, wrong types and bools-as-ints are refused.
    ``tcp_port`` is required for ``tcp`` and refused for ``terminal``."""
    if not isinstance(block, dict):
        return [_err(field, "must_be_object")]
    errors: list[dict] = []
    for k in block:
        if k not in {"mode", "tcp_port", "command", "idle_timeout_seconds"}:
            errors.append(_err(f"{field}.{k}", "unknown_field"))
    mode = block.get("mode")
    if mode not in SESSION_MODES:
        errors.append(_err(f"{field}.mode", "must_be_terminal_or_tcp"))
    port = block.get("tcp_port")
    if mode == "tcp":
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            errors.append(_err(f"{field}.tcp_port", "must_be_integer_1024_to_65535"))
    elif port is not None:
        errors.append(_err(f"{field}.tcp_port", "only_allowed_with_mode_tcp"))
    cmd = block.get("command")
    if cmd is not None:
        if not isinstance(cmd, list) or not 1 <= len(cmd) <= SESSION_MAX_ARGS:
            errors.append(_err(f"{field}.command", f"must_be_list_of_1_to_{SESSION_MAX_ARGS}_strings"))
        else:
            for i, a in enumerate(cmd):
                if not isinstance(a, str) or len(a) > SESSION_MAX_ARG_LEN or "\x00" in a or (i == 0 and not a):
                    errors.append(_err(f"{field}.command[{i}]", "invalid_argument"))
    idle = block.get("idle_timeout_seconds")
    if idle is not None and (not isinstance(idle, int) or isinstance(idle, bool)
                             or not SESSION_IDLE_MIN <= idle <= SESSION_IDLE_MAX):
        errors.append(_err(f"{field}.idle_timeout_seconds", "must_be_integer_60_to_86400"))
    return errors


SERVICE_MAX_CONCURRENCY = 64


def validate_service_block(block: Any, field: str = "manifest.service") -> list[dict]:
    """Kit service block: ``{"port": 1-65535, "health_path": "/...", "max_concurrency"?: 1-64}``. Strict."""
    from app.services.service_runtime import valid_path

    if not isinstance(block, dict):
        return [_err(field, "must_be_object")]
    errors: list[dict] = []
    for k in block:
        if k not in {"port", "health_path", "max_concurrency"}:
            errors.append(_err(f"{field}.{k}", "unknown_field"))
    port = block.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        errors.append(_err(f"{field}.port", "must_be_integer_1_to_65535"))
    hp = block.get("health_path")
    if not isinstance(hp, str) or not valid_path(hp):
        errors.append(_err(f"{field}.health_path", "invalid_health_path"))
    mc = block.get("max_concurrency")
    if mc is not None and (not isinstance(mc, int) or isinstance(mc, bool) or not 1 <= mc <= SERVICE_MAX_CONCURRENCY):
        errors.append(_err(f"{field}.max_concurrency", f"must_be_integer_1_to_{SERVICE_MAX_CONCURRENCY}"))
    return errors


def validate_manifest(manifest: Any) -> list[dict]:
    """Structural checks on a manifest. Returns a list of field errors (empty = fine)."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    errors: list[dict] = []
    if not isinstance(manifest, dict):
        return [_err("manifest", "must_be_object")]
    if len(canonical_manifest_bytes(manifest)) > MAX_MANIFEST_BYTES:
        return [_err("manifest", "manifest_too_large")]
    allowed = {"schema", "slug", "version", "name", "description", "category", "image", "mode", "param_schema",
               "inputs", "outputs", "resource_defaults", "billing_hints", "verification", "split",
               "egress", "assets", "checkpoint", "session", "service"}
    for k in manifest:
        if k not in allowed:
            errors.append(_err(f"manifest.{k}", "unknown_field"))
    if manifest.get("schema") != 1:
        errors.append(_err("manifest.schema", "unsupported_schema_version"))
    if not isinstance(manifest.get("slug"), str) or not SLUG_RE.match(manifest["slug"]):
        errors.append(_err("manifest.slug", "invalid_slug"))
    if not isinstance(manifest.get("version"), str) or not VERSION_RE.match(manifest["version"]):
        errors.append(_err("manifest.version", "invalid_version"))
    if not isinstance(manifest.get("name"), str) or not 1 <= len(manifest["name"]) <= 128:
        errors.append(_err("manifest.name", "invalid_name"))
    if manifest.get("category") not in CATEGORIES:
        errors.append(_err("manifest.category", "invalid_category"))
    if not isinstance(manifest.get("image"), str) or not IMAGE_RE.fullmatch(manifest["image"]):
        errors.append(_err("manifest.image", "image_must_be_pinned_by_sha256_digest"))
    if manifest.get("mode") not in SUPPORTED_MODES:
        errors.append(_err("manifest.mode", "mode_not_supported_yet"))

    for key, extra in (("inputs", ("kind", "required")), ("outputs", ("path",))):
        seen: set[str] = set()
        items = manifest.get(key)
        if not isinstance(items, list) or len(items) > 32:
            errors.append(_err(f"manifest.{key}", "must_be_list_of_at_most_32"))
            continue
        for i, it in enumerate(items):
            f = f"manifest.{key}[{i}]"
            if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not NAME_RE.match(it["name"]):
                errors.append(_err(f"{f}.name", "invalid_name"))
                continue
            if it["name"] in seen:
                errors.append(_err(f"{f}.name", "duplicate_name"))
            seen.add(it["name"])
            if key == "outputs":
                p = it.get("path")
                if not isinstance(p, str) or not p.startswith("/out/") or ".." in p.split("/"):
                    errors.append(_err(f"{f}.path", "output_path_must_be_under_/out/"))

    ps = manifest.get("param_schema")
    if not isinstance(ps, dict):
        errors.append(_err("manifest.param_schema", "must_be_object"))
    else:
        if len(canonical_manifest_bytes(ps)) > MAX_PARAM_SCHEMA_BYTES:
            errors.append(_err("manifest.param_schema", "param_schema_too_large"))
        if ps.get("type") != "object" or ps.get("additionalProperties") is not False:
            errors.append(_err("manifest.param_schema", "must_be_object_with_additionalProperties_false"))
        _check_refs(ps, "manifest.param_schema", errors)
        try:
            Draft202012Validator.check_schema(ps)
        except SchemaError as exc:
            errors.append(_err("manifest.param_schema", "invalid_json_schema", str(exc.message)[:300]))

    rd = manifest.get("resource_defaults")
    if not isinstance(rd, dict):
        errors.append(_err("manifest.resource_defaults", "must_be_object"))
    else:
        for k in rd:
            if k not in {"gpu_count", "min_vram_gb", "min_compute_capability", "cpu", "ram_gb", "disk_gb"}:
                errors.append(_err(f"manifest.resource_defaults.{k}", "unknown_field"))
        for k in ("gpu_count", "min_vram_gb", "cpu", "ram_gb", "disk_gb"):
            v = rd.get(k)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                errors.append(_err(f"manifest.resource_defaults.{k}", "must_be_non_negative_integer"))
    bh = manifest.get("billing_hints")
    if bh is not None and (not isinstance(bh, dict) or not isinstance(bh.get("unit"), str)
                           or not re.fullmatch(r"[a-z][a-z_]{0,31}", bh["unit"])):
        errors.append(_err("manifest.billing_hints.unit", "invalid_unit"))
    sp = manifest.get("split")  # U4: how a fan-out may cut this Kit's work; each shape names the param it patches
    if sp is not None:
        if not isinstance(sp, dict) or not sp:
            errors.append(_err("manifest.split", "must_be_non_empty_object"))
        else:
            for kind, rule in sp.items():
                f = f"manifest.split.{kind}"
                if kind not in ("range", "items", "bytes"):
                    errors.append(_err(f, "unknown_field"))
                elif not isinstance(rule, dict) or set(rule) != {"param"} or not isinstance(rule["param"], str) \
                        or not NAME_RE.match(rule["param"]):
                    errors.append(_err(f, "must_be_object_with_param_name"))
    errors.extend(_validate_runtime_sections(manifest))
    mode = manifest.get("mode")
    if "session" in manifest:
        errors.extend(validate_session_block(manifest["session"]))
        if mode in SUPPORTED_MODES and mode != "session":
            errors.append(_err("manifest.session", "session_block_requires_mode_session"))
    elif mode == "session":
        errors.append(_err("manifest.session", "session_block_required_for_mode_session"))
    if "service" in manifest:
        errors.extend(validate_service_block(manifest["service"]))
        if mode in SUPPORTED_MODES and mode != "service":
            errors.append(_err("manifest.service", "service_block_requires_mode_service"))
    elif mode == "service":
        errors.append(_err("manifest.service", "service_block_required_for_mode_service"))
    vm = manifest.get("verification")
    if vm is not None and (not isinstance(vm, dict) or not isinstance(vm.get("method"), str)):
        errors.append(_err("manifest.verification.method", "invalid_method"))
    return errors


# ---- registry ------------------------------------------------------------------------------

async def publish_kit(db: AsyncSession, *, manifest: dict, signature: str, published_by: uuid.UUID | None) -> Kit:
    # Signature first: nothing unsigned is even inspected further.
    key_id = verify_manifest_signature(manifest, signature)
    errors = validate_manifest(manifest)
    if errors:
        raise KitError("invalid_manifest", 422, errors)
    existing = (await db.execute(
        select(Kit.id).where(Kit.slug == manifest["slug"], Kit.version == manifest["version"])
    )).first()
    if existing:
        raise KitError("kit_version_exists", 409, [_err("manifest.version", "kit_version_exists")])
    kit = Kit(
        slug=manifest["slug"], version=manifest["version"], name=manifest["name"], category=manifest["category"],
        image_digest=manifest["image"], manifest=manifest,
        manifest_sha256=hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest(),
        signature=signature.lower(), signing_key_id=key_id, status="published", published_by=published_by,
    )
    db.add(kit)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise KitError("kit_version_exists", 409, [_err("manifest.version", "kit_version_exists")])
    return kit


async def get_kit(db: AsyncSession, slug: str, version: str) -> Kit | None:
    return (await db.execute(select(Kit).where(Kit.slug == slug, Kit.version == version))).scalar_one_or_none()


async def yank_kit(db: AsyncSession, slug: str, version: str) -> Kit:
    kit = await get_kit(db, slug, version)
    if kit is None:
        raise KitError("kit_not_found", 404)
    if kit.status != "yanked":
        kit.status = "yanked"
        kit.yanked_at = utcnow()
        await db.flush()
    return kit


def assert_kit_intact(kit: Kit) -> None:
    """Re-verify a stored Kit before use: hash, signature, and that the indexed columns match the manifest."""
    m = kit.manifest
    if hashlib.sha256(canonical_manifest_bytes(m)).hexdigest() != kit.manifest_sha256:
        raise KitError("kit_integrity_failed", 422, [_err("kit", "kit_integrity_failed")])
    verify_manifest_signature(m, kit.signature)
    if (m.get("slug"), m.get("version"), m.get("image")) != (kit.slug, kit.version, kit.image_digest):
        raise KitError("kit_integrity_failed", 422, [_err("kit", "kit_integrity_failed")])


# ---- Job Spec v2 validation ----------------------------------------------------------------

@dataclass
class ValidatedSpec:
    kit: Kit
    spec: JobSpecV2
    effective_resources: dict


def _pydantic_errors(exc: ValidationError) -> list[dict]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "spec"
        code = "unknown_field" if e["type"] == "extra_forbidden" else e["type"]
        out.append(_err(loc, code, str(e["msg"])[:300]))
    return out


async def validate_job_spec_v2(
    db: AsyncSession, raw: Any, *, allow_parallelism: bool = False, allowed_kit_modes: frozenset[str] = BATCH_MODES
) -> ValidatedSpec:
    from jsonschema import Draft202012Validator

    if not isinstance(raw, dict):
        raise KitError("invalid_spec", 422, [_err("spec", "must_be_object")])
    errors: list[dict] = []
    if raw.get("mode") == "byoc":
        raise KitError("invalid_spec", 422, [_err("mode", "use_post_v1_jobs_byoc", "send BYOC specs to POST /v1/jobs/byoc")])
    for k in NOT_SUPPORTED_YET:
        if k in raw:
            errors.append(_err(k, "not_supported_yet", f"'{k}' is not available in this version"))
    try:
        spec = JobSpecV2.model_validate({k: v for k, v in raw.items() if k not in NOT_SUPPORTED_YET})
    except ValidationError as exc:
        raise KitError("invalid_spec", 422, errors + _pydantic_errors(exc))
    if spec.mode == "vm":  # U14: the owner's go/no-go; refused (never ignored) unless turned on
        from app.core.config import settings as _settings

        if not _settings.WINDOWS_VM_ENABLED:
            errors.append(_err("mode", "windows_vm_disabled", "the Windows VM tier is not enabled on this platform"))
        elif spec.vm is not None and spec.vm.gpu_passthrough and spec.resources.gpu.count is None:
            pass  # GPU count falls back to the Kit default; the scheduler additionally requires a vfio_gpu host
    if spec.parallelism is not None and not allow_parallelism:
        errors.append(_err("parallelism", "not_supported_yet", "use POST /v1/jobs/v2/fanout for a fan-out job"))
    if errors:
        raise KitError("invalid_spec", 422, errors)

    slug, version = spec.kit_slug_version()
    kit = await get_kit(db, slug, version)
    if kit is None or kit.status != "published":
        raise KitError("invalid_spec", 422, [_err("kit", "kit_not_found_or_unavailable")])
    assert_kit_intact(kit)
    m = kit.manifest
    kit_mode = m.get("mode", "batch")
    if kit_mode not in allowed_kit_modes:
        # service kits start only through POST /v1/services, session kits only through POST /v1/sessions.
        code = "mode_not_batch" if "batch" in allowed_kit_modes else f"mode_not_{sorted(allowed_kit_modes)[0]}"
        raise KitError("invalid_spec", 422, [_err("mode", code, f"this Kit has mode {kit_mode!r} and cannot be started this way")])

    # Params against the Kit's schema.
    for e in sorted(Draft202012Validator(m["param_schema"]).iter_errors(spec.params), key=lambda e: list(e.path)):
        path = ".".join(["params", *[str(p) for p in e.absolute_path]])
        if e.validator == "additionalProperties":
            code = "unknown_field"
        else:
            code = f"schema_{e.validator}"
        errors.append(_err(path, code, str(e.message)[:300]))

    # Inputs and outputs against the Kit's slots.
    slot_in = {i["name"]: i for i in m["inputs"]}
    given = set()
    for idx, i in enumerate(spec.inputs):
        if i.name not in slot_in:
            errors.append(_err(f"inputs[{idx}].name", "unknown_input"))
        elif i.name in given:
            errors.append(_err(f"inputs[{idx}].name", "duplicate_name"))
        given.add(i.name)
    for name, slot in slot_in.items():
        if slot.get("required", True) and name not in given:
            errors.append(_err("inputs", "missing_required_input", f"input '{name}' is required"))
    slot_out = {o["name"] for o in m["outputs"]}
    seen_out: set[str] = set()
    for idx, o in enumerate(spec.outputs):
        if o.name not in slot_out:
            errors.append(_err(f"outputs[{idx}].name", "unknown_output"))
        elif o.name in seen_out:
            errors.append(_err(f"outputs[{idx}].name", "duplicate_name"))
        seen_out.add(o.name)

    # Resources may raise the Kit's minimums, never lower them.
    rd = m["resource_defaults"]
    eff = {k: rd.get(k) for k in ("gpu_count", "min_vram_gb", "min_compute_capability", "cpu", "ram_gb", "disk_gb")}
    g, r = spec.resources.gpu, spec.resources
    for field, req, key in (("resources.gpu.count", g.count, "gpu_count"),
                            ("resources.gpu.min_vram_gb", g.min_vram_gb, "min_vram_gb"),
                            ("resources.cpu", r.cpu, "cpu"), ("resources.ram_gb", r.ram_gb, "ram_gb"),
                            ("resources.disk_gb", r.disk_gb, "disk_gb")):
        if req is None:
            continue
        if eff[key] is not None and req < eff[key]:
            errors.append(_err(field, "below_kit_minimum", f"Kit requires at least {eff[key]}"))
        else:
            eff[key] = req
    if r.nodes and r.nodes > 1:
        eff["node_count"] = int(r.nodes)  # U13 (absent for single-node specs: existing effective_resources unchanged)
    cc = g.min_compute_capability
    if cc is not None:
        if eff["min_compute_capability"] is not None and float(cc) < float(eff["min_compute_capability"]):
            errors.append(_err("resources.gpu.min_compute_capability", "below_kit_minimum"))
        else:
            eff["min_compute_capability"] = cc

    unit = (m.get("billing_hints") or {}).get("unit")
    if spec.billing.unit is not None and spec.billing.unit != unit:
        errors.append(_err("billing.unit", "unit_not_offered_by_kit", f"Kit unit is {unit!r}"))

    if errors:
        raise KitError("invalid_spec", 422, errors)
    return ValidatedSpec(kit=kit, spec=spec, effective_resources=eff)
