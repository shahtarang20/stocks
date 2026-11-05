"""Private Kit jobs on the agent (U18, private fan-out parts). Plaintext lives only in RAM and only inside the hardened container.

Flow (after the existing attestation, key-share release and in-RAM Data Key decrypt in ``privacy.execute_sealed_job``):

1. the claim's v2 block is the MINIMAL private plan: ``{"private": true, "kit": {...}, "outputs": [{name, path, required}]}``
   and nothing else (no inputs, assets, egress, checkpoint, URL). Anything more is refused (fail closed);
2. a work dir is made on RAM-only storage (tmpfs/ramfs, verified from /proc/mounts; none = refuse, never disk);
3. the decrypted input (``{"inputs": {name: {"b64"|"text": ...}}, "params": {...}}``; a dict with neither key is taken as
   ``params``) is staged there, mounted read-only at ``/in/<name>`` into the Kit container with NO network (privacy
   tiers never get any), ``/out`` is a RAM dir too, params arrive as ``/out/input.json`` like every Kit;
4. each declared output is read from ``/out`` (regular files, or a dir packed into a tar; symlinks refused, size capped),
   SEALED to the job's ``result_public_key`` (ccpp-v1 ``seal()``, AAD ``ccpp-v1|job-output-file|<job>|<name>``) and only
   the ciphertext is uploaded through the data plane; the plaintext never leaves the host;
5. everything in the work dir is zeroed and removed (also on failure), then ``execute_sealed_job`` signs the wipe receipt.

The dict this module returns is what ``execute_sealed_job`` seals as the job's ``encrypted_output`` (the customer opens
it to learn each output's file id, ciphertext hash and plaintext hash).

Honest limits: Python cannot erase every copy of immutable ``bytes``; swap and a hostile host root are outside this
code (that is the Sealed OS and the attestation tier). Sealing is one-shot, so a sealed output is capped at
``MAX_SEALED_OUTPUT_BYTES``. Never run against a real container in this slice.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .privacy_crypto import VERSION, canonical_json, seal

PRIVATE_TIERS = ("sealed", "sealed_pro", "confidential")
RAM_FS_TYPES = ("tmpfs", "ramfs")
MAX_PRIVATE_INPUT_BYTES = 256 * 1024 * 1024
MAX_SEALED_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_OUTPUTS = 32
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_KIT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ALLOWED_PLAN_KEYS = {"spec_version", "private", "kit", "outputs"}


def output_file_aad(job_id: str, name: str) -> bytes:
    """AAD of one sealed Kit output file of a private job (binds job and output name). Kept here, not in
    privacy_crypto.py, which must stay byte-identical across the API, agent and SDK copies."""
    return f"{VERSION}|job-output-file|{job_id}|{name}".encode()


def pad_stream_plaintext(data: bytes, block: int) -> bytes:
    """Frame a sealed stream chunk to a fixed block size: ``u32 big-endian length || data || zero bytes`` up to a multiple of
    ``block``. ``block <= 0`` leaves the chunk raw. The relay sees only ciphertext sizes, so every chunk of up to
    ``block - 4`` bytes then looks the same. The customer reads the length, then takes that many bytes."""
    if block <= 0:
        return data
    framed = len(data).to_bytes(4, "big") + data
    return framed + b"\x00" * (-len(framed) % block)


class PrivateKitError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- plan
@dataclass
class PrivatePlan:
    kit: str
    outputs: list[dict[str, Any]]


def parse_private_plan(v2: dict[str, Any]) -> PrivatePlan:
    """Validate the minimal private plan. Any extra key (inputs, assets, egress, checkpoint, byoc, service, vm...) is refused."""
    if not isinstance(v2, dict) or v2.get("private") is not True:
        raise PrivateKitError("v2_not_supported_for_privacy_tier", "a private job may only carry the minimal private plan")
    extra = set(v2) - _ALLOWED_PLAN_KEYS
    if extra:
        raise PrivateKitError("invalid_private_plan", f"unexpected plan fields: {', '.join(sorted(extra))}")
    kit = v2.get("kit")
    slug = kit.get("slug") if isinstance(kit, dict) else None
    if not isinstance(slug, str) or not _KIT.match(slug):
        raise PrivateKitError("invalid_private_plan", "bad kit")
    raw = v2.get("outputs")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_OUTPUTS:
        raise PrivateKitError("invalid_private_plan", "a private kit job needs 1..32 declared outputs")
    outputs: list[dict[str, Any]] = []
    for o in raw:
        if not isinstance(o, dict) or not _NAME.match(str(o.get("name", ""))):
            raise PrivateKitError("invalid_private_plan", "bad output name")
        path = str(o.get("path", ""))
        if not path or path.startswith("/") or ".." in path.split("/") or "\x00" in path:
            raise PrivateKitError("invalid_private_plan", "bad output path")
        outputs.append({"name": o["name"], "path": path, "required": bool(o.get("required", True))})
    if len({o["name"] for o in outputs}) != len(outputs):
        raise PrivateKitError("invalid_private_plan", "duplicate output names")
    return PrivatePlan(slug, outputs)


# --------------------------------------------------------------------------- RAM-only work dir
def _mount_type(path: str, mounts_file: str = "/proc/mounts") -> Optional[str]:
    """Filesystem type of the mount that holds ``path`` (longest matching mount point), or None if unknown."""
    real = os.path.realpath(path)
    best: tuple[int, str] | None = None
    try:
        with open(mounts_file, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
                    if best is None or len(mp) > best[0]:
                        best = (len(mp), parts[2])
    except OSError:
        return None
    return best[1] if best else None


def ram_workdir(prefix: str, base: Optional[str] = None, *, mounts_file: str = "/proc/mounts") -> str:
    """A fresh 0700 directory on RAM-only storage. Fails closed: if no candidate is tmpfs/ramfs there is NO fallback to disk."""
    candidates = [base] if base else ["/dev/shm", f"/run/user/{os.getuid()}"]
    for cand in candidates:
        if cand and os.path.isdir(cand) and _mount_type(cand, mounts_file) in RAM_FS_TYPES:
            d = tempfile.mkdtemp(prefix=prefix, dir=cand)
            os.chmod(d, 0o700)
            return d
    raise PrivateKitError("ram_workdir_unavailable", "no RAM-only (tmpfs) work directory is available on this host")


def wipe_dir(path: str, *, keep_root: bool = False) -> None:
    """Zero every regular file's bytes, then remove the tree. Best effort; never raises."""
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for n in files:
                p = os.path.join(root, n)
                try:
                    if os.path.islink(p) or not os.path.isfile(p):
                        continue
                    os.chmod(p, 0o600)
                    size = os.path.getsize(p)
                    with open(p, "r+b") as fh:
                        left = size
                        zeros = b"\x00" * min(1 << 20, max(size, 1))
                        while left > 0:
                            fh.write(zeros[: min(left, len(zeros))])
                            left -= len(zeros)
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError:
                    continue
    finally:
        if keep_root:
            for n in os.listdir(path) if os.path.isdir(path) else []:
                p = os.path.join(path, n)
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        else:
            shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- staging
def stage_private_inputs(decrypted: Any, in_dir: str) -> tuple[dict[str, Any], list[str]]:
    """Write the decrypted files into ``in_dir`` (0444). Returns (params, file names)."""
    if not isinstance(decrypted, dict):
        raise PrivateKitError("invalid_private_input", "the decrypted input must be a JSON object")
    if "inputs" in decrypted or "params" in decrypted:
        files, params = decrypted.get("inputs") or {}, decrypted.get("params") or {}
    else:
        files, params = {}, decrypted
    if not isinstance(files, dict) or not isinstance(params, dict) or len(files) > 32:
        raise PrivateKitError("invalid_private_input", "inputs must be an object (at most 32 files), params an object")
    os.makedirs(in_dir, exist_ok=True)
    total, names = 0, []
    for name, body in files.items():
        if not _NAME.match(str(name)) or not isinstance(body, dict):
            raise PrivateKitError("invalid_private_input", "bad input name")
        if "b64" in body:
            try:
                data = base64.b64decode(str(body["b64"]), validate=True)
            except (binascii.Error, ValueError):
                raise PrivateKitError("invalid_private_input", "bad base64 input")
        elif isinstance(body.get("text"), str):
            data = body["text"].encode("utf-8")
        else:
            raise PrivateKitError("invalid_private_input", "an input needs 'b64' or 'text'")
        total += len(data)
        if total > MAX_PRIVATE_INPUT_BYTES:
            raise PrivateKitError("private_input_too_large", "the private input exceeds the RAM staging cap")
        path = os.path.join(in_dir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        names.append(name)
    return params, names


# --------------------------------------------------------------------------- run
@dataclass
class PrivateKitContext:
    plan: PrivatePlan
    ram_dir: str
    api: Any  # runtime_v2.V2Api (data plane calls, machine-signed)
    transport: Any = None
    sleeper: Optional[Callable[[float], None]] = None
    metrics: dict[str, Any] = field(default_factory=dict)


def _collect_sealed_outputs(ctx: PrivateKitContext, job_id: str, result_public_key: str, out_dir: str) -> list[dict[str, Any]]:
    from .runtime_v2 import hash_file, pack_dir, upload_file, V2Error

    sealed_dir = os.path.join(ctx.ram_dir, "sealed")
    os.makedirs(sealed_dir, exist_ok=True)
    root = os.path.realpath(out_dir)
    entries: list[dict[str, Any]] = []
    for o in ctx.plan.outputs:
        path = os.path.join(root, o["path"])
        real = os.path.realpath(path)
        if not (real == root or real.startswith(root + os.sep)) or os.path.islink(path):
            raise PrivateKitError("unsafe_output", "an output escapes the output dir")
        if not os.path.exists(real):
            if o["required"]:
                raise PrivateKitError("missing_output", "a required output was not produced")
            continue
        src, kind = real, "file"
        if os.path.isdir(real):
            src, kind = os.path.join(ctx.ram_dir, f"pack-{o['name']}.tar"), "tar"
            pack_dir(real, src)
        elif not os.path.isfile(real):
            raise PrivateKitError("unsafe_output", "an output is not a regular file")
        if os.path.getsize(src) > MAX_SEALED_OUTPUT_BYTES:
            raise PrivateKitError("output_too_large", "a sealed output exceeds the one-shot sealing cap")
        plain_sha, plain_size = hash_file(src)
        with open(src, "rb") as fh:
            envelope = seal(result_public_key, fh.read(), output_file_aad(job_id, o["name"]))
        ct_path = os.path.join(sealed_dir, f"{o['name']}.sealed.json")
        with open(ct_path, "wb") as fh:
            fh.write(canonical_json(envelope))
        try:
            desc = upload_file(ctx.api, job_id, ct_path, purpose="output", name=o["name"],
                               content_type="application/json", transport=ctx.transport,
                               **({"sleeper": ctx.sleeper} if ctx.sleeper else {}))
        except V2Error as exc:
            raise PrivateKitError(exc.code, "sealed output upload failed")
        entries.append({
            "name": o["name"], "kind": kind, "file_id": desc["file_id"],
            "ciphertext_sha256": desc["sha256"], "ciphertext_size": desc["size"],
            "plaintext_sha256": plain_sha, "plaintext_size": plain_size,
            "aad": output_file_aad(job_id, o["name"]).decode(),
        })
    return entries


def run_private_kit(
    spec: Any,
    decrypted: dict[str, Any],
    ctx: PrivateKitContext,
    executor_selector: Callable[..., Any],
    capabilities: Any,
) -> dict[str, Any]:
    """Run the Kit container on the decrypted input, seal and upload its outputs, wipe. Returns the dict to seal as encrypted_output."""
    from .executor import JobSpec

    if (spec.isolation_tier or "").lower() not in PRIVATE_TIERS:
        raise PrivateKitError("not_a_private_job", "run_private_kit is for privacy tiers only")
    if spec.execution_mode != "hardware" or not spec.image_digest:
        raise PrivateKitError("kit_requires_hardware", "a private Kit job needs a hardware run of the Kit's pinned image")
    if spec.network_policy:
        raise PrivateKitError("private_job_network_refused", "privacy-tier jobs never get network access")
    in_dir, out_dir = os.path.join(ctx.ram_dir, "in"), os.path.join(ctx.ram_dir, "out")
    try:
        params, names = stage_private_inputs(decrypted, in_dir)
        os.makedirs(out_dir, exist_ok=True)
        os.chmod(out_dir, 0o777)
        sub = JobSpec(
            job_id=spec.job_id, workload_type="kit", model=spec.model, input={"params": params},
            execution_mode="hardware", image_digest=spec.image_digest, limits=spec.limits,
            isolation_tier=spec.isolation_tier, network_policy=None,
            v2={"kit": {"slug": ctx.plan.kit}}, private_kit=True,
            mounts=[{"source": os.path.join(in_dir, n), "target": f"/in/{n}", "readonly": True} for n in names],
            extra_env=({"COMPUTECOIN_INPUTS_DIR": "/in"} if names else {}), output_dir=out_dir,
        )
        res = executor_selector(sub, capabilities).run(sub)
        if res.status != "success":
            # Generic on purpose: executor text could quote plaintext.
            raise PrivateKitError("kit_execution_failed", "the Kit container did not finish successfully")
        ctx.metrics.update(res.metrics or {})
        outputs = _collect_sealed_outputs(ctx, spec.job_id, spec.result_public_key, out_dir)
        return {"kit": ctx.plan.kit, "outputs": outputs}
    finally:
        wipe_dir(ctx.ram_dir, keep_root=True)
