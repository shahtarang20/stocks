"""Adapter from NVIDIA's documented attestation tool output to the verifier's inputs (cross-platform plan, NVIDIA gap).

The platform verifier (``apps/api/app/services/hardware_attestation/nvidia.py``) checks RAW evidence itself, against
pinned NVIDIA roots and signed RIMs: an SPDM 1.1 GET_MEASUREMENTS ``request``, its ``response``, and the device
certificate ``chain``. NVIDIA's documented ``attest`` command instead prints an attestation RESULT (an Entity Attestation
Token / Result, a signed JWT whose claims say "the GPU passed"). A result carries NO raw SPDM messages, so it cannot be
turned into verifier input, and a host's own "passed" verdict is not evidence (L1, never read a security claim from a
host-produced verdict). This module therefore accepts exactly the output forms that DO contain evidence and refuses the
rest with a precise error:

=====================================  =========================================================================
Tool output                            Handling
=====================================  =========================================================================
``{request, response, certificate_chain}`` (hex + PEM list)   our wrapper contract: passed through
Evidence list ``[{evidence, certificate, arch, nonce?}]`` (base64)   adapted: ``evidence`` = 37-byte SPDM request ++ response
Attestation RESULT (detached EAT array, JWT, EAR)           REFUSED: it holds no SPDM evidence (``NvidiaResultError``)
anything else                                               REFUSED (``NvidiaEatError``)
=====================================  =========================================================================

Documentation status (X11): the overview page below was last read 2026-11-03 by the earlier confidential-tier work and was NOT
re-read for this module (no network in this change). Unverified:

* NVIDIA Attestation SDK overview, https://docs.nvidia.com/attestation/nv-attestation-sdk-cpp/latest/overview.html:
  ``attest`` yields an attestation result. TODO(verify-docs): the exact JSON/JWT layout of that result, the claim names
  (``eat_nonce``, ``x-nvidia-overall-att-result``, ``submods``/``ear.status``) and a ``collect-evidence`` command with
  JSON output are written from the documented concepts and the nvTrust sample layout, NOT confirmed against a tool run.
* TODO(verify-docs): that an NVIDIA ``evidence`` blob is ``SPDM request (37 bytes) ++ SPDM response``. The split is only
  accepted when the 37-byte prefix is a signed GET_MEASUREMENTS bound to this nonce; otherwise it fails closed.
* TODO(verify-docs): evidence-list entry order equals GPU index.

Nothing here verifies a signature or trusts a claim: the JWT payloads in a result are decoded ONLY to name what the tool
returned in an error message. Everything that reaches the platform is raw evidence, verified server side.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

SPDM_REQUEST_LEN = 37  # SPDM 1.1 GET_MEASUREMENTS with a nonce: 4 header + 32 nonce + 1 slot id
MAX_OUTPUT_BYTES = 4 * 1024 * 1024  # a tool that prints more than this is refused before parsing
MAX_ENTRIES = 64
MAX_CHAIN_CERTS = 8
_PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_COLLATERAL_KEYS = ("crls", "ocsp_responses", "rims")
_RESULT_MARKERS = ("eat_nonce", "x-nvidia-overall-att-result", "submods", "ear.status", "eat_profile", "x-nvidia-ver")


class NvidiaEatError(ValueError):
    """The tool output cannot be adapted. Always fail closed."""


class NvidiaResultError(NvidiaEatError):
    """The tool returned an attestation RESULT (EAT / EAR), which contains no raw SPDM evidence."""


@dataclass
class NvidiaEvidence:
    request: bytes
    response: bytes
    certificate_chain: list[str]
    collateral: dict[str, list[str]] = field(default_factory=dict)
    source: str = "contract"  # contract | evidence_list


@dataclass
class ResultSummary:
    """UNTRUSTED description of an attestation result, for error text and logs only."""

    overall_result: Optional[bool] = None
    nonce: Optional[str] = None
    claims_seen: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
def _b64(value: Any, what: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise NvidiaEatError(f"{what} is missing or not a string")
    try:
        text = value.strip()
        text += "=" * (-len(text) % 4)
        urlsafe = "-" in text or "_" in text
        return base64.urlsafe_b64decode(text) if urlsafe else base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise NvidiaEatError(f"{what} is not valid base64") from exc


def _hex(value: Any, what: str) -> bytes:
    if not isinstance(value, str):
        raise NvidiaEatError(f"{what} is missing or not a hex string")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise NvidiaEatError(f"{what} is not valid hex") from exc


def pem_blocks(text: str) -> list[str]:
    """The PEM certificates in ``text``, normalised to end with a newline. At most MAX_CHAIN_CERTS."""
    blocks = [m.group(0).strip() + "\n" for m in _PEM_RE.finditer(text)]
    if len(blocks) > MAX_CHAIN_CERTS:
        raise NvidiaEatError(f"certificate chain has more than {MAX_CHAIN_CERTS} certificates")
    return blocks


def _chain_from(value: Any) -> list[str]:
    """A certificate chain given as a PEM string, a base64 of a PEM string, or a list of PEM strings."""
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_chain_from(item))
        if len(out) > MAX_CHAIN_CERTS:
            raise NvidiaEatError(f"certificate chain has more than {MAX_CHAIN_CERTS} certificates")
        return out
    if not isinstance(value, str) or not value.strip():
        raise NvidiaEatError("device certificate chain is missing")
    if "BEGIN CERTIFICATE" in value:
        blocks = pem_blocks(value)
        if not blocks:
            raise NvidiaEatError("device certificate chain holds no PEM certificate")
        return blocks
    decoded = _b64(value, "certificate").decode("utf-8", "replace")  # base64 of PEM text
    blocks = pem_blocks(decoded)
    if not blocks:
        raise NvidiaEatError("device certificate chain holds no PEM certificate")
    return blocks


def _collateral(raw: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key in _COLLATERAL_KEYS:
        items = raw.get(key)
        if items:
            if not isinstance(items, list) or len(items) > MAX_ENTRIES:
                raise NvidiaEatError(f"{key} must be a short list")
            out[key] = [str(x) for x in items]
    return out


# --------------------------------------------------------------------------- results (refused, but described)
def _jwt_payload(token: str) -> Optional[dict]:
    if not isinstance(token, str) or not _JWT_RE.match(token):
        return None
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return data if isinstance(data, dict) else None
    except (binascii.Error, ValueError):
        return None


def _collect_payloads(doc: Any, depth: int = 0) -> list[dict]:
    """Every JWT payload / claims dict reachable in a result document (bounded). UNTRUSTED."""
    if depth > 4:
        return []
    found: list[dict] = []
    if isinstance(doc, str):
        payload = _jwt_payload(doc)
        if payload is not None:
            found.append(payload)
    elif isinstance(doc, dict):
        found.append(doc)
        for v in list(doc.values())[:MAX_ENTRIES]:
            found.extend(_collect_payloads(v, depth + 1))
    elif isinstance(doc, list):
        for v in doc[:MAX_ENTRIES]:
            found.extend(_collect_payloads(v, depth + 1))
    return found


def looks_like_result(doc: Any) -> bool:
    if isinstance(doc, str):
        return _jwt_payload(doc.strip()) is not None
    return any(
        isinstance(p, dict) and any(m in p for m in _RESULT_MARKERS) for p in _collect_payloads(doc)
    )


def summarize_result(doc: Any) -> ResultSummary:
    """Name what an attestation result says (UNVERIFIED: no signature is checked). For messages and logs only."""
    summary = ResultSummary()
    seen: set[str] = set()
    for p in _collect_payloads(doc):
        for key in _RESULT_MARKERS:
            if key in p:
                seen.add(key)
        overall = p.get("x-nvidia-overall-att-result")
        if isinstance(overall, bool) and summary.overall_result is None:
            summary.overall_result = overall
        status = p.get("ear.status")
        if isinstance(status, str) and summary.overall_result is None:
            summary.overall_result = status == "affirming"
        nonce = p.get("eat_nonce")
        if isinstance(nonce, str) and summary.nonce is None:
            summary.nonce = nonce[:128]
    summary.claims_seen = sorted(seen)
    return summary


def refuse_result(doc: Any) -> "NvidiaResultError":
    s = summarize_result(doc)
    verdict = "unknown" if s.overall_result is None else ("pass" if s.overall_result else "fail")
    return NvidiaResultError(
        "NVIDIA tool returned an attestation RESULT (host-side verdict: "
        f"{verdict}) which contains no raw SPDM evidence; the platform verifies raw evidence itself and never trusts a "
        "host verdict. Configure nvidia_attest_cmd with a wrapper that prints the evidence (see "
        "docs/operations/nvidia-attestation-wrapper.md)"
    )


# --------------------------------------------------------------------------- evidence forms
def _from_contract(raw: dict) -> NvidiaEvidence:
    chain = raw.get("certificate_chain")
    if not isinstance(chain, list) or not chain or len(chain) > MAX_CHAIN_CERTS:
        raise NvidiaEatError("certificate_chain must be a short list of PEM certificates")
    return NvidiaEvidence(
        request=_hex(raw.get("request"), "request"),
        response=_hex(raw.get("response"), "response"),
        certificate_chain=[str(p) for p in chain],
        collateral=_collateral(raw),
        source="contract",
    )


def _entries(doc: Any) -> Optional[list[dict]]:
    """The evidence-list entries in ``doc`` (a list, or a dict wrapping one), or None if ``doc`` is not an evidence list."""
    if isinstance(doc, dict):
        for key in ("evidence_list", "evidences", "evidence"):
            inner = doc.get(key)
            if isinstance(inner, list):
                doc = inner
                break
        else:
            if isinstance(doc.get("evidence"), str):  # one bare entry
                doc = [doc]
    if isinstance(doc, list) and doc and all(isinstance(e, dict) and "evidence" in e for e in doc):
        if len(doc) > MAX_ENTRIES:
            raise NvidiaEatError("evidence list is too long")
        return doc
    return None


def _from_evidence_entry(entry: dict, nonce: bytes) -> NvidiaEvidence:
    blob = _b64(entry.get("evidence"), "evidence")
    if len(blob) <= SPDM_REQUEST_LEN:
        raise NvidiaEatError("evidence is too short to hold an SPDM request and response")
    claimed = entry.get("nonce")
    if claimed is not None and (not isinstance(claimed, str) or _hex(claimed, "nonce") != nonce):
        raise NvidiaEatError("evidence entry nonce is not the nonce of this challenge")
    cert_value = entry.get("certificate") or entry.get("certificate_chain")
    return NvidiaEvidence(
        request=blob[:SPDM_REQUEST_LEN],
        response=blob[SPDM_REQUEST_LEN:],
        certificate_chain=_chain_from(cert_value),
        collateral=_collateral(entry),
        source="evidence_list",
    )


# --------------------------------------------------------------------------- entry point
def normalize_tool_output(stdout: bytes, *, nonce: bytes, gpu_index: int = 0) -> NvidiaEvidence:
    """Parse the evidence tool's stdout into verifier inputs. Raises :class:`NvidiaEatError` for anything else.

    ``nonce`` is the 32-byte SHA-256 of the challenge the caller passed to the tool. The caller still applies the SPDM
    message-type and nonce-binding checks to the result (``tee.NvidiaGpuCollector``)."""
    if not isinstance(stdout, (bytes, bytearray)):
        raise NvidiaEatError("tool output is not bytes")
    if len(stdout) > MAX_OUTPUT_BYTES:
        raise NvidiaEatError("tool output is larger than the allowed size")
    try:
        text = bytes(stdout).decode("utf-8")
        doc = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        stripped = bytes(stdout).decode("utf-8", "replace").strip()
        if _jwt_payload(stripped) is not None:  # a bare JWT on stdout: an attestation result
            raise refuse_result(stripped) from None
        raise NvidiaEatError("NVIDIA attestation tool output is not JSON") from None

    if isinstance(doc, dict) and "request" in doc and "response" in doc and "certificate_chain" in doc:
        return _from_contract(doc)
    entries = _entries(doc)
    if entries is not None:
        if not 0 <= gpu_index < len(entries):
            raise NvidiaEatError(f"tool returned {len(entries)} evidence entries, no entry for GPU index {gpu_index}")
        return _from_evidence_entry(entries[gpu_index], nonce)
    if looks_like_result(doc):
        raise refuse_result(doc)
    raise NvidiaEatError(
        "NVIDIA attestation tool output is not {request, response, certificate_chain}, an evidence list, or a recognised result"
    )
