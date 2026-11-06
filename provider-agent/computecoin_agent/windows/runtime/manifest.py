"""Signed runtime manifest (``runtime-manifest.json``): the only source of what goes into the managed distro (X10).

File format (an envelope; the manifest is read ONLY from the signed bytes, never from a field next to them):

    {"format": "computecoin-runtime-manifest/1",
     "key_id": "<free text, informational only>",
     "payload": "<base64 of the UTF-8 JSON manifest bytes>",
     "signature": "<base64 Ed25519 signature>"}

The signature covers ``SIGNING_DOMAIN + payload_bytes``. The public key is PINNED in the runtime settings (32 raw bytes,
base64); an empty or malformed pin refuses everything (L1 fail closed). A manifest whose ``sequence`` is lower than the
one already installed is refused by the provisioner (anti-rollback).

Manifest payload (all fields required unless marked optional; unknown fields are ignored but never trusted):

    {"schema": 1, "sequence": 12, "version": "2026.11.1", "arch": "amd64", "min_wsl_version": "2.1.5",
     "rootfs": {"url": "https://...", "sha256": "<64 hex>", "size_bytes": 123456789},
     "docker": {"repo": {...}, "packages": {"docker-ce": "...", "docker-ce-cli": "...", "containerd.io": "..."},
                "engine_version": "27.3.1"},
     "nvidia_toolkit": {"repo": {...}, "version": "1.17.0",
                        "packages": {"nvidia-container-toolkit": "...", "nvidia-container-toolkit-base": "...",
                                     "libnvidia-container-tools": "...", "libnvidia-container1": "..."}},
     "runner": {"version": "...", "wheel_filename": "computecoin_agent-....whl", "wheel_url": "https://...",
                "wheel_sha256": "...", "lock_url": "https://...", "lock_sha256": "..."},
     "egress_proxy": {"image": "name@sha256:..."},
     "smoke_test": {"image": "name@sha256:..."}}

``repo`` = {"uri": "https://...", "suite": "..." (or "/" for a flat repository), "component": "..." (omitted when flat),
            "key_url": "https://...", "key_fingerprint": "<40 hex>"}. The apt source line is BUILT from these fields; no free-form line is accepted.

TODO(verify-docs): Docker apt repository layout and signing key fingerprint, and the NVIDIA container toolkit apt
repository and the version pinning of its four packages, must be taken from the vendors' current install documentation
when the first manifest is produced (X11). Nothing here was checked against them.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

SIGNING_DOMAIN = b"computecoin-runtime-manifest/1\n"
ENVELOPE_FORMAT = "computecoin-runtime-manifest/1"
SUPPORTED_SCHEMA = 1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FPR = re.compile(r"^[0-9A-Fa-f]{40}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,127}$")  # first char alnum: never parsed as an option
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\.whl$")
_IMAGE_DIGEST = re.compile(r"^[a-z0-9][a-z0-9./_-]{0,200}@sha256:[0-9a-f]{64}$")
_HTTPS = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?$")

DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io")
NVIDIA_PACKAGES = (
    "nvidia-container-toolkit", "nvidia-container-toolkit-base", "libnvidia-container-tools", "libnvidia-container1",
)


class ManifestError(ValueError):
    """The manifest is unsigned, tampered, malformed or unsupported. Always fatal (L1)."""


class SignatureError(ManifestError):
    """The Ed25519 signature check failed, or no usable public key is pinned."""


@dataclass(frozen=True)
class AptRepo:
    uri: str
    suite: str  # "/" means a flat repository (no component), as the NVIDIA container toolkit repository is laid out
    component: Optional[str]
    key_url: str
    key_fingerprint: str  # upper-case, 40 hex


@dataclass(frozen=True)
class RuntimeManifest:
    sequence: int
    version: str
    arch: str
    min_wsl_version: str
    rootfs_url: str
    rootfs_sha256: str
    rootfs_size_bytes: Optional[int]
    docker_repo: AptRepo
    docker_packages: dict[str, str]
    docker_engine_version: str
    nvidia_repo: AptRepo
    nvidia_packages: dict[str, str]
    nvidia_toolkit_version: str
    runner_version: str
    runner_wheel_filename: str
    runner_wheel_url: str
    runner_wheel_sha256: str
    runner_lock_url: str
    runner_lock_sha256: str
    egress_proxy_image: str
    smoke_test_image: str
    payload_sha256: str  # sha256 of the signed payload bytes: identifies this exact manifest

    def pins(self) -> dict[str, Any]:
        """Everything that decides the content of the distro (used to fingerprint provisioning steps)."""
        return {
            "rootfs": self.rootfs_sha256, "docker": self.docker_packages, "nvidia": self.nvidia_packages,
            "runner": [self.runner_version, self.runner_wheel_sha256, self.runner_lock_sha256],
            "egress_proxy": self.egress_proxy_image, "smoke_image": self.smoke_test_image,
            "docker_repo": self.docker_repo.__dict__, "nvidia_repo": self.nvidia_repo.__dict__,
        }


# --------------------------------------------------------------------------- #
# Signature
# --------------------------------------------------------------------------- #
def decode_public_key(value: str) -> bytes:
    """The pinned key: 32 raw bytes as base64 (or 64 hex chars). Anything else is refused."""
    text = (value or "").strip()
    if not text:
        raise SignatureError("no manifest public key is pinned; refusing every manifest")
    raw: Optional[bytes] = None
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        raw = bytes.fromhex(text)
    else:
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raw = None
    if raw is None or len(raw) != 32:
        raise SignatureError("the pinned manifest public key must be 32 bytes (base64 or 64 hex characters)")
    if raw == bytes(32):
        raise SignatureError("the pinned manifest public key is all zero; refusing")
    return raw


def verify_signature(payload: bytes, signature: bytes, public_key: bytes) -> None:
    """Raise SignatureError unless ``signature`` is a valid Ed25519 signature over ``SIGNING_DOMAIN + payload``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, SIGNING_DOMAIN + payload)
    except InvalidSignature as exc:
        raise SignatureError("runtime manifest signature does not verify against the pinned key") from exc
    except ValueError as exc:  # malformed key or signature length
        raise SignatureError(f"runtime manifest signature check could not run: {exc}") from exc


def sign_manifest(payload: bytes, private_key_bytes: bytes, key_id: str = "") -> bytes:
    """Release-tool helper (and test helper): build the envelope for ``payload``. The private key is never stored here."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sig = Ed25519PrivateKey.from_private_bytes(private_key_bytes).sign(SIGNING_DOMAIN + payload)
    return json.dumps({
        "format": ENVELOPE_FORMAT, "key_id": key_id,
        "payload": base64.b64encode(payload).decode("ascii"), "signature": base64.b64encode(sig).decode("ascii"),
    }, sort_keys=True).encode("utf-8")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _req(d: Any, key: str, kind: type, where: str) -> Any:
    if not isinstance(d, dict) or key not in d:
        raise ManifestError(f"manifest field missing: {where}{key}")
    v = d[key]
    if not isinstance(v, kind) or isinstance(v, bool) and kind is int:
        raise ManifestError(f"manifest field has the wrong type: {where}{key}")
    return v


def _https(v: str, name: str) -> str:
    if not _HTTPS.match(v):
        raise ManifestError(f"{name} must be an https URL")
    return v


def _repo(d: Any, where: str) -> AptRepo:
    uri = _https(_req(d, "uri", str, where), where + "uri")
    suite = _req(d, "suite", str, where)
    component = d.get("component")
    if suite == "/":
        if component not in (None, ""):
            raise ManifestError(f"{where}a flat repository (suite '/') has no component")
        component = None
    elif not _TOKEN.match(suite) or not isinstance(component, str) or not _TOKEN.match(component):
        raise ManifestError(f"{where}suite/component contain unexpected characters")
    fpr = _req(d, "key_fingerprint", str, where)
    if not _FPR.match(fpr):
        raise ManifestError(f"{where}key_fingerprint must be 40 hex characters")
    return AptRepo(uri.rstrip("/"), suite, component, _https(_req(d, "key_url", str, where), where + "key_url"), fpr.upper())


def _packages(d: Any, required: tuple[str, ...], where: str) -> dict[str, str]:
    pk = _req(d, "packages", dict, where)
    out: dict[str, str] = {}
    for name in required:
        v = _req(pk, name, str, where + "packages.")
        if not _VERSION.match(v):
            raise ManifestError(f"{where}packages.{name} is not a valid pinned version")
        out[name] = v
    extra = set(pk) - set(required)
    if extra:
        raise ManifestError(f"{where}packages has unexpected entries: {sorted(extra)}")
    return out


def _sha(v: str, name: str) -> str:
    if not _SHA256.match(v):
        raise ManifestError(f"{name} must be 64 lower-case hex characters")
    return v


def parse_manifest_payload(payload: bytes, *, expected_arch: str = "amd64") -> RuntimeManifest:
    import hashlib

    try:
        d = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError("manifest payload is not valid UTF-8 JSON") from exc
    if not isinstance(d, dict):
        raise ManifestError("manifest payload must be a JSON object")
    if _req(d, "schema", int, "") != SUPPORTED_SCHEMA:
        raise ManifestError("unsupported manifest schema")
    seq = _req(d, "sequence", int, "")
    if seq < 0:
        raise ManifestError("sequence must be >= 0")
    arch = _req(d, "arch", str, "")
    if arch != expected_arch:
        raise ManifestError(f"manifest is for {arch!r}, this host needs {expected_arch!r}")
    version = _req(d, "version", str, "")
    min_wsl = _req(d, "min_wsl_version", str, "")
    if not _VERSION.match(version) or not re.match(r"^[0-9]+(\.[0-9]+){1,3}$", min_wsl):
        raise ManifestError("version or min_wsl_version is malformed")

    rootfs = _req(d, "rootfs", dict, "")
    size = rootfs.get("size_bytes")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        raise ManifestError("rootfs.size_bytes must be a positive integer")
    docker = _req(d, "docker", dict, "")
    nv = _req(d, "nvidia_toolkit", dict, "")
    runner = _req(d, "runner", dict, "")
    wheel = _req(runner, "wheel_filename", str, "runner.")
    if not _FILENAME.match(wheel):
        raise ManifestError("runner.wheel_filename is not a plain wheel file name")
    egress = _req(_req(d, "egress_proxy", dict, ""), "image", str, "egress_proxy.")
    smoke = _req(_req(d, "smoke_test", dict, ""), "image", str, "smoke_test.")
    for name, img in (("egress_proxy.image", egress), ("smoke_test.image", smoke)):
        if not _IMAGE_DIGEST.match(img):
            raise ManifestError(f"{name} must be pinned by @sha256: digest")
    engine = _req(docker, "engine_version", str, "docker.")
    nv_ver = _req(nv, "version", str, "nvidia_toolkit.")
    runner_ver = _req(runner, "version", str, "runner.")
    for name, v in (("docker.engine_version", engine), ("nvidia_toolkit.version", nv_ver), ("runner.version", runner_ver)):
        if not _VERSION.match(v):
            raise ManifestError(f"{name} is malformed")

    return RuntimeManifest(
        sequence=seq, version=version, arch=arch, min_wsl_version=min_wsl,
        rootfs_url=_https(_req(rootfs, "url", str, "rootfs."), "rootfs.url"),
        rootfs_sha256=_sha(_req(rootfs, "sha256", str, "rootfs."), "rootfs.sha256"),
        rootfs_size_bytes=size,
        docker_repo=_repo(_req(docker, "repo", dict, "docker."), "docker.repo."),
        docker_packages=_packages(docker, DOCKER_PACKAGES, "docker."), docker_engine_version=engine,
        nvidia_repo=_repo(_req(nv, "repo", dict, "nvidia_toolkit."), "nvidia_toolkit.repo."),
        nvidia_packages=_packages(nv, NVIDIA_PACKAGES, "nvidia_toolkit."), nvidia_toolkit_version=nv_ver,
        runner_version=runner_ver, runner_wheel_filename=wheel,
        runner_wheel_url=_https(_req(runner, "wheel_url", str, "runner."), "runner.wheel_url"),
        runner_wheel_sha256=_sha(_req(runner, "wheel_sha256", str, "runner."), "runner.wheel_sha256"),
        runner_lock_url=_https(_req(runner, "lock_url", str, "runner."), "runner.lock_url"),
        runner_lock_sha256=_sha(_req(runner, "lock_sha256", str, "runner."), "runner.lock_sha256"),
        egress_proxy_image=egress, smoke_test_image=smoke,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_verified_manifest(envelope_bytes: bytes, pinned_public_key: str, *, expected_arch: str = "amd64") -> RuntimeManifest:
    """Verify the signature FIRST, then parse the signed bytes. Fails closed on every problem."""
    key = decode_public_key(pinned_public_key)
    try:
        env = json.loads(envelope_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError("runtime-manifest.json is not valid JSON") from exc
    if not isinstance(env, dict) or env.get("format") != ENVELOPE_FORMAT:
        raise ManifestError("runtime-manifest.json has an unknown envelope format")
    try:
        payload = base64.b64decode(str(env.get("payload", "")), validate=True)
        signature = base64.b64decode(str(env.get("signature", "")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("runtime-manifest.json payload or signature is not base64") from exc
    if not payload or not signature:
        raise SignatureError("runtime manifest is unsigned")
    verify_signature(payload, signature, key)
    return parse_manifest_payload(payload, expected_arch=expected_arch)
