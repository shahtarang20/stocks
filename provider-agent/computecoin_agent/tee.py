"""Confidential-tier hardware evidence (P4): NVIDIA GPU SPDM, AMD SEV-SNP and Intel TDX.

Produces exactly what the API's verifiers accept (apps/api/app/services/hardware_attestation):

- GPU:     ``{"request", "response", "certificate_chain"}`` plus the revocation / reference collateral
           the wrapper tool supplies (``crls``, ``ocsp_responses``, ``rims``); the SPDM GET_MEASUREMENTS
           nonce is SHA-256(challenge).
- SEV-SNP: ``{"report", "vcek_cert", "ask_cert", "crls"}``; REPORT_DATA = SHA-512(gpu_bound_nonce).
- TDX:     ``{"quote", "collateral": {"tcb_info", "qe_identity", "tcb_signing_chain", "crls"}}``;
           REPORTDATA = SHA-512(gpu_bound_nonce).

Revocation collateral (P2 / R1 / R5): the API refuses a certificate path that has no current,
issuer-signed CRL or OCSP response (``*_revocation_unknown``), so the agent MUST carry the CRLs the
issuers publish. The agent only relays them (a CRL is trusted by its signature, never its origin);
CRLs the agent cannot find are simply omitted and the API then refuses the evidence (fail closed).

Order (ccpp-v1 6f): GPU evidence first, then the CPU report over ``gpu_bound_nonce(challenge, gpu)``
so CPU and GPU evidence from two machines cannot be combined.

Every collector is injectable (runner, file roots, fetcher) like ``tpm.py``. A collector that cannot
produce real evidence raises ``TeeError``; the agent never sends a placeholder (R1), and attestation
fails.

Docs check (R7), 2026-11-03 (details: docs/operations/external-interfaces-checked.md):
- configfs-tsm: matches https://www.kernel.org/doc/Documentation/ABI/testing/configfs-tsm
- SNP report offsets (REPORT_DATA 0x50, REPORTED_TCB 0x180, CHIP_ID 0x1A0, size 0x4A0) match AMD
  publication 56860 as implemented in github.com/google/go-sev-guest (abi/abi.go); AMD KDS URLs and
  TCB byte layout match go-sev-guest kds/kds.go and AMD pub. 57230. Turin differs: see SevSnpCollector.
- Intel PCS v4 TDX endpoints and header names match
  https://cc-enabling.trustedservices.intel.com/intel-sgx-tdx-pccs/03/api_specification_for_pccs/
- NVIDIA tool interface: UNVERIFIABLE, see NvidiaGpuCollector.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from .attestation import AttestationClientError
from .nvidia_eat import NvidiaEatError, normalize_tool_output
from .platform import get_platform

TSM_REPORT_ROOT = get_platform().hardware.tsm_report_root()  # "" where the platform has no configfs-tsm
SNP_REPORT_SIZE = 0x4A0
SGX_EXT_OID = "1.2.840.113741.1.13.1"
PEM_RE = re.compile(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n?", re.S)

Fetcher = Callable[[str], "tuple[bytes, dict[str, str]]"]  # url -> (body, lower-cased headers)


class TeeError(AttestationClientError):
    """A TEE / GPU collector could not produce real evidence."""


CRL_PEM_RE = re.compile(rb"-----BEGIN X509 CRL-----.*?-----END X509 CRL-----\n?", re.S)


def _cached_crls(cache_dir: Optional[str], name: str) -> "list[str]":
    """PEM CRLs from ``cache_dir/name`` (operator-supplied from the issuers' published CRLs). Missing
    file -> []; the API then refuses the evidence as ``*_revocation_unknown``. A CRL that does not
    parse is an error here rather than being relayed."""
    if not cache_dir or not Path(cache_dir, name).is_file():
        return []
    out = []
    for pem in CRL_PEM_RE.findall(Path(cache_dir, name).read_bytes()):
        try:
            x509.load_pem_x509_crl(pem)
        except ValueError as exc:
            raise TeeError(f"{name} contains a CRL that does not parse") from exc
        out.append(pem.decode())
    return out


def gpu_bound_nonce(nonce: str, gpu_evidence: dict[str, Any]) -> str:
    """The value a Confidential host's CPU TEE report must commit to (via SHA-512 in REPORTDATA):
    the challenge AND the GPU's signed measurement response, so CPU and GPU evidence from two
    different machines cannot be combined."""
    gpu_digest = hashlib.sha256(str(gpu_evidence.get("response", "")).lower().encode("ascii")).hexdigest()
    return f"{nonce}|gpu:{gpu_digest}"


def _http_fetch(url: str) -> "tuple[bytes, dict[str, str]]":
    import httpx

    try:
        res = httpx.get(url, timeout=30.0)
    except httpx.HTTPError as exc:
        raise TeeError(f"fetching {url} failed: {exc}") from exc
    if res.status_code != 200:
        raise TeeError(f"fetching {url} failed: HTTP {res.status_code}")
    return res.content, {k.lower(): v for k, v in res.headers.items()}


# --------------------------------------------------------------------------- NVIDIA GPU
class NvidiaGpuCollector:
    """SPDM GET_MEASUREMENTS evidence from an NVIDIA GPU in confidential-computing mode.

    Tool output is read by ``nvidia_eat.normalize_tool_output`` (contract JSON, or NVIDIA's evidence list; an attestation
    RESULT is refused). The operator wrapper contract is docs/operations/nvidia-attestation-wrapper.md.

    ``command`` is NVIDIA's evidence tool. TODO(verify-docs): UNVERIFIED. NVIDIA's documented CLI
    (``nvattest attest --device gpu --verifier local``, https://docs.nvidia.com/attestation/nv-attestation-sdk-cpp/latest/overview.html,
    checked 2026-11-03) is documented to emit an Entity Attestation Result, not raw SPDM
    request/response, and ``--gpu-index`` / ``--nonce`` / this JSON shape are not documented anywhere
    I could find. So ``command`` must be an operator-supplied wrapper that honours the contract below
    (built on the Attestation SDK's evidence-collection API); check it against the installed SDK
    version before enabling nvidia_cc. It is run as
    ``command + ["--gpu-index", N, "--nonce", <hex SHA-256(challenge)>]`` and must print JSON
    ``{"request": hex, "response": hex, "certificate_chain": [PEM, ...]}`` on stdout. The output is
    checked here too (right SPDM message types, nonce really SHA-256(challenge)), so a wrong or stale
    tool output raises instead of being sent.
    """

    flavor = "nvidia_cc"

    def __init__(self, command: list[str], *, gpu_index: int = 0, runner: Callable[..., Any] = subprocess.run):
        if not command:
            raise TeeError("nvidia_cc is on but no attestation command is configured (nvidia_attest_cmd)")
        self.command, self.gpu_index, self._run = list(command), int(gpu_index), runner

    def collect(self, challenge: str) -> dict[str, Any]:
        nonce = hashlib.sha256(challenge.encode("utf-8")).digest()
        args = self.command + ["--gpu-index", str(self.gpu_index), "--nonce", nonce.hex()]
        try:
            proc = self._run(args, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TeeError(f"NVIDIA attestation tool failed to run: {exc}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise TeeError(f"NVIDIA attestation tool failed: {err[-1] if err else proc.returncode}")
        try:
            # The adapter accepts our wrapper contract and NVIDIA's evidence-list form, and refuses an attestation
            # RESULT (it holds no SPDM evidence). Optional revocation / reference collateral (OCSP responses or CRLs for
            # the device chain, signed RIMs) is relayed untouched: the API verifies each by signature against its pins.
            ev = normalize_tool_output(proc.stdout or b"", nonce=nonce, gpu_index=self.gpu_index)
        except NvidiaEatError as exc:
            raise TeeError(str(exc)) from exc
        request, response, chain, collateral = ev.request, ev.response, ev.certificate_chain, ev.collateral
        if len(request) != 37 or request[0] != 0x11 or request[1] != 0xE0 or not request[2] & 1:
            raise TeeError("GPU request is not a signed SPDM 1.1 GET_MEASUREMENTS")
        if request[4:36] != nonce:
            raise TeeError("GPU request is not bound to this challenge")
        if len(response) < 8 or response[0] != 0x11 or response[1] != 0x60:
            raise TeeError("GPU response is not an SPDM 1.1 MEASUREMENTS")
        if not chain or not all("BEGIN CERTIFICATE" in p for p in chain):
            raise TeeError("GPU device certificate chain is missing")
        return {"request": request.hex(), "response": response.hex(), "certificate_chain": chain, **collateral}


# --------------------------------------------------------------------------- configfs-tsm
def tsm_report(
    report_data: bytes, *, expect_provider: str, root: str = TSM_REPORT_ROOT,
    remover: Callable[[str], None] = os.rmdir,
) -> bytes:
    """One configfs-tsm report: write ``inblob`` (64 bytes), read ``outblob``.

    Checked 2026-11-03 against https://www.kernel.org/doc/Documentation/ABI/testing/configfs-tsm
    (inblob write-only <= 64 bytes, outblob read-only, provider names the outblob format e.g.
    sev_guest / tdx_guest, generation increments on every inblob/option write). Entry creation is
    mkdir under the report root (kernel configfs-tsm convention; the ABI page does not spell it out).
    ``generation`` is read before and after so a concurrent writer is noticed.
    """
    if len(report_data) != 64:
        raise TeeError("report data must be 64 bytes")
    if not root:
        raise TeeError("configfs-tsm is not available on this platform")  # never mkdir relative to the working directory
    entry = os.path.join(root, f"computecoin-{uuid.uuid4().hex}")
    try:
        os.mkdir(entry)
    except OSError as exc:
        raise TeeError(f"configfs-tsm is not available at {root}: {exc}") from exc
    try:
        def read(name: str) -> bytes:
            return Path(entry, name).read_bytes()

        Path(entry, "inblob").write_bytes(report_data)
        generation = read("generation").strip()
        blob = read("outblob")
        provider = read("provider").decode("utf-8", "replace").strip()
        if read("generation").strip() != generation:
            raise TeeError("configfs-tsm report was overwritten by another writer")
        if provider != expect_provider:
            raise TeeError(f"configfs-tsm provider is {provider!r}, expected {expect_provider!r}")
        if not blob:
            raise TeeError("configfs-tsm returned an empty report")
        return blob
    except OSError as exc:
        raise TeeError(f"configfs-tsm report failed: {exc}") from exc
    finally:
        try:
            remover(entry)
        except OSError:
            pass


# --------------------------------------------------------------------------- SEV-SNP
class SevSnpCollector:
    """SEV-SNP guest report + VCEK and ASK. Certificates come from ``cache_dir`` (``vcek.pem``,
    ``ask.pem``) when present, otherwise from AMD KDS via ``fetcher``. The CRL(s) for the ASK come from
    ``cache_dir/crl.pem`` (operator-supplied; fetching from KDS is not implemented because the CRL
    endpoint is not yet checked against AMD's docs, R7); without one the API refuses the evidence."""

    flavor = "sev_snp"
    # Checked 2026-11-03: base URL and /vcek/v1/{product}/{chip_id_hex}?blSPL&teeSPL&snpSPL&ucodeSPL and
    # /vcek/v1/{product}/cert_chain (PEM, ASK then ARK) per AMD pub. 57230 as implemented in
    # github.com/google/go-sev-guest kds/kds.go. Turin (family 1Ah) uses a different TCB layout plus
    # fmcSPL and its HWID length could not be confirmed, so Turin is refused (see _certs).
    KDS = "https://kdsintf.amd.com"

    def __init__(self, *, product: Optional[str] = None, cache_dir: Optional[str] = None, tsm_root: str = TSM_REPORT_ROOT,
                 fetcher: Fetcher = _http_fetch, remover: Callable[[str], None] = os.rmdir,
                 report_fn: Optional[Callable[[bytes], bytes]] = None):
        self.product, self.cache_dir, self.tsm_root = product, cache_dir, tsm_root
        self._fetch, self._remover, self._report_fn = fetcher, remover, report_fn

    def collect(self, bound_nonce: str) -> dict[str, Any]:
        report_data = hashlib.sha512(bound_nonce.encode("utf-8")).digest()
        report = (self._report_fn or (lambda rd: tsm_report(
            rd, expect_provider="sev_guest", root=self.tsm_root, remover=self._remover)))(report_data)
        if len(report) != SNP_REPORT_SIZE:
            raise TeeError(f"SEV-SNP report must be {SNP_REPORT_SIZE} bytes, got {len(report)}")
        if report[0x50:0x90] != report_data:
            raise TeeError("SEV-SNP report does not carry the requested REPORT_DATA")
        vcek, ask = self._certs(report)
        evidence = {"report": report.hex(), "vcek_cert": vcek, "ask_cert": ask}
        crls = _cached_crls(self.cache_dir, "crl.pem")
        if crls:
            evidence["crls"] = crls
        return evidence

    def _cached(self, name: str) -> Optional[str]:
        if self.cache_dir and Path(self.cache_dir, name).is_file():
            return Path(self.cache_dir, name).read_text()
        return None

    def _certs(self, report: bytes) -> "tuple[str, str]":
        vcek, ask = self._cached("vcek.pem"), self._cached("ask.pem")
        if vcek and ask:
            return vcek, ask
        if not self.product:
            raise TeeError("no VCEK/ASK cached and no SEV-SNP product (Milan/Genoa/Turin) configured for AMD KDS")
        if self.product.strip().lower() not in ("milan", "genoa"):
            # Turin: TCB bytes are fmc=0, bl=1, tee=2, snp=3, ucode=7 plus an fmcSPL parameter, and the
            # HWID length is unconfirmed. Do not guess: use cached vcek.pem / ask.pem instead.
            raise TeeError(f"AMD KDS fetch is only implemented for Milan and Genoa, not {self.product!r}; provide cached certs")
        # Milan/Genoa: CHIP_ID at 0x1A0 (64 bytes); REPORTED_TCB at 0x180: bootloader=byte0, tee=byte1,
        # snp=byte6, microcode=byte7 (go-sev-guest abi.go / kds.go, checked 2026-11-03).
        chip_id, tcb = report[0x1A0:0x1E0].hex(), report[0x180:0x188]
        url = (f"{self.KDS}/vcek/v1/{self.product}/{chip_id}?blSPL={tcb[0]:02d}&teeSPL={tcb[1]:02d}"
               f"&snpSPL={tcb[6]:02d}&ucodeSPL={tcb[7]:02d}")
        der, _ = self._fetch(url)
        try:
            vcek = x509.load_der_x509_certificate(der).public_bytes(Encoding.PEM).decode()
        except ValueError as exc:
            raise TeeError("AMD KDS returned an unusable VCEK certificate") from exc
        chain, _ = self._fetch(f"{self.KDS}/vcek/v1/{self.product}/cert_chain")
        pems = PEM_RE.findall(chain)
        if len(pems) < 2:
            raise TeeError("AMD KDS cert_chain did not contain ASK and ARK")
        return vcek, pems[0].decode()  # the ARK is pinned by the platform, never sent


# --------------------------------------------------------------------------- TDX
def _der_items(data: bytes) -> "list[tuple[int, bytes]]":
    out, pos = [], 0
    while pos < len(data):
        tag, first = data[pos], data[pos + 1]
        pos += 2
        if first & 0x80:
            n = first & 0x7F
            length, pos = int.from_bytes(data[pos:pos + n], "big"), pos + n
        else:
            length = first
        if pos + length > len(data):
            raise ValueError("truncated DER")
        out.append((tag, data[pos:pos + length]))
        pos += length
    return out


def _der_oid(data: bytes) -> str:
    parts, value = [], 0
    for b in data[1:]:
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(value)
            value = 0
    first = data[0]
    return ".".join(str(p) for p in [first // 40, first % 40, *parts])


def fmspc_from_quote(quote: bytes) -> str:
    """FMSPC from the PCK certificate embedded in the quote's certification data."""
    pems = PEM_RE.findall(quote)
    if not pems:
        raise TeeError("TDX quote carries no PCK certificate chain")
    try:
        pck = x509.load_pem_x509_certificate(pems[0])
        ext = pck.extensions.get_extension_for_oid(x509.ObjectIdentifier(SGX_EXT_OID)).value
        (_, top), = _der_items(ext.value)
        for _, entry in _der_items(top):
            (_, oid_b), (_, value) = _der_items(entry)
            if _der_oid(oid_b) == SGX_EXT_OID + ".4":
                return value.hex()
    except (ValueError, x509.ExtensionNotFound, IndexError) as exc:
        raise TeeError("PCK certificate has no SGX extension with an FMSPC") from exc
    raise TeeError("PCK certificate has no FMSPC")


class TdxCollector:
    """TDX quote via configfs-tsm plus Intel collateral. Collateral comes from ``cache_dir``
    (``tcb_info.json``, ``qe_identity.json``, ``tcb_signing_chain.pem``) when complete, else Intel PCS.
    The Root CA and PCK CA CRLs come from ``cache_dir/crls.pem`` (operator-supplied; PCS CRL fetching is
    not implemented, R7); without them the API refuses the evidence as ``tdx_revocation_unknown``."""

    flavor = "tdx"
    # Checked 2026-11-03: GET {PCS}/tcb?fmspc= and GET {PCS}/qe/identity, per Intel's PCCS API spec
    # https://cc-enabling.trustedservices.intel.com/intel-sgx-tdx-pccs/03/api_specification_for_pccs/
    PCS = "https://api.trustedservices.intel.com/tdx/certification/v4"

    def __init__(self, *, cache_dir: Optional[str] = None, tsm_root: str = TSM_REPORT_ROOT,
                 fetcher: Fetcher = _http_fetch, remover: Callable[[str], None] = os.rmdir,
                 report_fn: Optional[Callable[[bytes], bytes]] = None):
        self.cache_dir, self.tsm_root, self._fetch, self._remover = cache_dir, tsm_root, fetcher, remover
        self._report_fn = report_fn

    def collect(self, bound_nonce: str) -> dict[str, Any]:
        report_data = hashlib.sha512(bound_nonce.encode("utf-8")).digest()
        quote = (self._report_fn or (lambda rd: tsm_report(
            rd, expect_provider="tdx_guest", root=self.tsm_root, remover=self._remover)))(report_data)
        if len(quote) < 636 or struct.unpack_from("<HH", quote, 0) != (4, 2):
            raise TeeError("configfs-tsm did not return a v4 ECDSA-P256 TDX quote")
        # TD report REPORTDATA sits at header(48) + 520 in a v4 TDX quote body (TD report body is 584
        # bytes; layout from Intel's TDX DCAP quote spec, from memory: PDF text not readable here).
        if quote[48 + 520:48 + 584] != report_data:
            raise TeeError("TDX quote does not carry the requested REPORTDATA")
        collateral = self._collateral(quote)
        crls = _cached_crls(self.cache_dir, "crls.pem")
        if crls and "crls" not in collateral:
            collateral["crls"] = crls
        return {"quote": quote.hex(), "collateral": collateral}

    def _collateral(self, quote: bytes) -> dict[str, Any]:
        if self.cache_dir:
            d = Path(self.cache_dir)
            names = ("tcb_info.json", "qe_identity.json", "tcb_signing_chain.pem")
            if all((d / n).is_file() for n in names):
                return {"tcb_info": (d / names[0]).read_text(), "qe_identity": (d / names[1]).read_text(),
                        "tcb_signing_chain": [p.decode() for p in PEM_RE.findall((d / names[2]).read_bytes())]}
        # TCB-Info-Issuer-Chain (TCB signing cert, then root) is the documented v4 header name. The spec does
        # not state its encoding (only SGX-PCK-Certificate-Issuer-Chain is documented as URL-encoded);
        # unquote is a no-op on plain PEM, so both are handled. Checked 2026-11-03.
        tcb, h1 = self._fetch(f"{self.PCS}/tcb?fmspc={fmspc_from_quote(quote)}")
        qe, _ = self._fetch(f"{self.PCS}/qe/identity")
        chain = urllib.parse.unquote(h1.get("tcb-info-issuer-chain", "")).encode()
        pems = [p.decode() for p in PEM_RE.findall(chain)]
        if not pems:
            raise TeeError("Intel PCS returned no TCB signing chain")
        return {"tcb_info": tcb.decode("utf-8"), "qe_identity": qe.decode("utf-8"), "tcb_signing_chain": pems}


# --------------------------------------------------------------------------- provider
def confidential_provider(cpu: Any, gpu: Any) -> Callable[[str], "tuple[str, dict[str, Any]]"]:
    """``nonce -> ("confidential", {"cpu_flavor", "cpu", "gpu"})``. GPU first, then the CPU report
    over the GPU-bound nonce. Any collector failure propagates (no partial evidence)."""
    if cpu is None or gpu is None:
        raise TeeError("Confidential needs both a CPU TEE (tee_cpu) and an NVIDIA CC GPU (nvidia_cc)")

    def provide(nonce: str) -> "tuple[str, dict[str, Any]]":
        gpu_evidence = gpu.collect(nonce)
        cpu_evidence = cpu.collect(gpu_bound_nonce(nonce, gpu_evidence))
        return "confidential", {"cpu_flavor": cpu.flavor, "cpu": cpu_evidence, "gpu": gpu_evidence}

    return provide


def provider_from_config(config: Any) -> Callable[[str], "tuple[str, dict[str, Any]]"]:
    flavor = getattr(config, "tee_cpu", None)
    cache = getattr(config, "tee_cache_dir", None)
    if flavor == "sev_snp":
        cpu: Any = SevSnpCollector(product=getattr(config, "tee_snp_product", None), cache_dir=cache)
    elif flavor == "tdx":
        cpu = TdxCollector(cache_dir=cache)
    else:
        raise TeeError("Confidential needs tee_cpu = 'tdx' or 'sev_snp' in the agent config")
    if not getattr(config, "nvidia_cc", False):
        raise TeeError("Confidential needs nvidia_cc = true (an NVIDIA GPU in confidential-computing mode)")
    gpu = NvidiaGpuCollector(list(getattr(config, "nvidia_attest_cmd", []) or []))
    return confidential_provider(cpu, gpu)


def hardware_for_tier(config: Any, tier: str) -> Optional[Callable[[str], "tuple[str, dict[str, Any]]"]]:
    """The hardware-evidence provider for ``tier``: TEE evidence for confidential (raises when not
    configured, R1), the TPM quote otherwise."""
    if tier == "confidential":
        return provider_from_config(config)
    from .tpm import hardware_provider

    return hardware_provider(config.tpm())
