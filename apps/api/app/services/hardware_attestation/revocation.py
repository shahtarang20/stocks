"""Certificate revocation for the hardware verifiers (P2 / R5).

Revocation data is trusted by signature, never by where it came from, so the host may send it in
the evidence. For every link ``certificate -> issuer`` of a verified path (the pinned root itself is
removed by un-pinning it) the verifier needs ONE current statement from that issuer:

- a CRL signed by the issuer certificate, with ``thisUpdate <= now < nextUpdate`` (a CRL without
  ``nextUpdate``, a delta CRL, or one whose scope excludes the certificate is not usable), or
- an OCSP response (NVIDIA) signed by the issuer or by a delegated responder certificate that the
  issuer signed with the OCSPSigning usage, for exactly this certificate, current.

A listed serial (or an OCSP ``revoked``) refuses with ``<prefix>_certificate_revoked``. A link with
no usable statement refuses with ``<prefix>_revocation_unknown``. A forged, expired or wrong-issuer
CRL is simply not usable, so it can never make a certificate look good.

Vendor formats (R7; to be re-checked against current docs before real evidence is accepted):
Intel PCS ``/sgx/certification/v4/pckcrl`` and ``/sgx/certification/v4/rootcacrl`` (PEM or hex DER),
AMD KDS ``/vcek/v1/{product}/crl`` (DER, signed by the ASK), NVIDIA OCSP (RFC 6960) / CRL.
``required="leaf"`` (AMD) demands a current CRL only for the leaf link; CRLs for upper links are
honoured when present.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
from collections.abc import Iterable
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509 import ocsp
from cryptography.x509.oid import ExtendedKeyUsageOID

from app.services.hardware_attestation import HardwareAttestationError

_HEX = re.compile(r"^[0-9a-fA-F]+$")
# Host-supplied evidence: every CRL / OCSP response is checked against every link of the chain (a signature verification
# each), so an unbounded list is a CPU lever. A real chain needs a handful.
MAX_COLLATERAL_ITEMS = 32


def _raw_bytes(raw: Any, prefix: str, what: str) -> bytes:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise HardwareAttestationError(f"{prefix}_bad_collateral", f"{what} is empty or not text")
    text = raw.strip()
    if text.startswith("-----BEGIN"):
        return text.encode("ascii", "replace")
    try:
        if len(text) % 2 == 0 and _HEX.match(text):
            return bytes.fromhex(text)
        return base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HardwareAttestationError(f"{prefix}_bad_collateral", f"{what} is not PEM, hex or base64") from exc


def parse_crls(raw_list: Any, prefix: str) -> list[x509.CertificateRevocationList]:
    """CRLs as PEM, hex DER or base64 DER. Malformed input is refused, not skipped."""
    if raw_list is None:
        return []
    if not isinstance(raw_list, (list, tuple)):
        raise HardwareAttestationError(f"{prefix}_bad_collateral", "crls must be a list")
    if len(raw_list) > MAX_COLLATERAL_ITEMS:
        raise HardwareAttestationError(f"{prefix}_bad_collateral", f"at most {MAX_COLLATERAL_ITEMS} CRLs are accepted")
    out = []
    for i, raw in enumerate(raw_list):
        data = _raw_bytes(raw, prefix, f"CRL {i}")
        try:
            out.append(x509.load_pem_x509_crl(data) if data.startswith(b"-----BEGIN") else x509.load_der_x509_crl(data))
        except ValueError as exc:
            raise HardwareAttestationError(f"{prefix}_bad_collateral", f"CRL {i} is not a valid X.509 CRL") from exc
    return out


def parse_ocsp_responses(raw_list: Any, prefix: str) -> list[ocsp.OCSPResponse]:
    if raw_list is None:
        return []
    if not isinstance(raw_list, (list, tuple)):
        raise HardwareAttestationError(f"{prefix}_bad_collateral", "ocsp_responses must be a list")
    if len(raw_list) > MAX_COLLATERAL_ITEMS:
        raise HardwareAttestationError(f"{prefix}_bad_collateral", f"at most {MAX_COLLATERAL_ITEMS} OCSP responses are accepted")
    out = []
    for i, raw in enumerate(raw_list):
        try:
            out.append(ocsp.load_der_ocsp_response(_raw_bytes(raw, prefix, f"OCSP response {i}")))
        except ValueError as exc:
            raise HardwareAttestationError(f"{prefix}_bad_collateral", f"OCSP response {i} is not DER") from exc
    return out


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _can_sign_crl(issuer: x509.Certificate) -> bool:
    try:
        return bool(issuer.extensions.get_extension_for_class(x509.KeyUsage).value.crl_sign)
    except x509.ExtensionNotFound:
        return True


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _crl_status(crl: x509.CertificateRevocationList, cert: x509.Certificate, issuer: x509.Certificate, now: dt.datetime) -> str | None:
    """"revoked", "good", or None when this CRL says nothing usable about ``cert``."""
    if crl.issuer != issuer.subject or cert.issuer != issuer.subject or not _can_sign_crl(issuer):
        return None
    try:
        if not crl.is_signature_valid(issuer.public_key()):
            return None
    except Exception:
        return None
    this_update = _aware(getattr(crl, "last_update_utc", None) or crl.last_update)
    next_update = _aware(getattr(crl, "next_update_utc", None) or crl.next_update)
    if this_update is None or next_update is None or not (this_update <= now < next_update):
        return None
    try:
        crl.extensions.get_extension_for_class(x509.DeltaCRLIndicator)
        return None  # a delta CRL is not a complete statement
    except x509.ExtensionNotFound:
        pass
    try:
        idp = crl.extensions.get_extension_for_class(x509.IssuingDistributionPoint).value
        if idp.indirect_crl or idp.only_contains_attribute_certs:
            return None
        if idp.only_contains_ca_certs and not _is_ca(cert):
            return None
        if idp.only_contains_user_certs and _is_ca(cert):
            return None
    except x509.ExtensionNotFound:
        pass
    return "revoked" if crl.get_revoked_certificate_by_serial_number(cert.serial_number) is not None else "good"


def _verify_ocsp_signature(resp: ocsp.OCSPResponse, issuer: x509.Certificate, now: dt.datetime) -> bool:
    candidates = [issuer]
    for c in resp.certificates:
        try:
            c.verify_directly_issued_by(issuer)
            eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except Exception:
            continue
        if ExtendedKeyUsageOID.OCSP_SIGNING in eku and c.not_valid_before_utc <= now <= c.not_valid_after_utc:
            candidates.append(c)
    hash_alg = resp.signature_hash_algorithm
    if hash_alg is None:
        return False
    for cand in candidates:
        key = cand.public_key()
        try:
            if isinstance(key, rsa.RSAPublicKey):
                key.verify(resp.signature, resp.tbs_response_bytes, padding.PKCS1v15(), hash_alg)
            elif isinstance(key, ec.EllipticCurvePublicKey):
                key.verify(resp.signature, resp.tbs_response_bytes, ec.ECDSA(hash_alg))
            else:
                continue
            return True
        except Exception:
            continue
    return False


def _ocsp_status(resp: ocsp.OCSPResponse, cert: x509.Certificate, issuer: x509.Certificate, now: dt.datetime) -> str | None:
    try:
        if resp.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL or resp.serial_number != cert.serial_number:
            return None
        ref = ocsp.OCSPRequestBuilder().add_certificate(cert, issuer, resp.hash_algorithm).build()
        if resp.issuer_key_hash != ref.issuer_key_hash or resp.issuer_name_hash != ref.issuer_name_hash:
            return None
        this_update = _aware(getattr(resp, "this_update_utc", None) or resp.this_update)
        next_update = _aware(getattr(resp, "next_update_utc", None) or resp.next_update)
        if this_update is None or next_update is None or not (this_update <= now < next_update):
            return None
        if not _verify_ocsp_signature(resp, issuer, now):
            return None
        if resp.certificate_status == ocsp.OCSPCertStatus.REVOKED:
            return "revoked"
        return "good" if resp.certificate_status == ocsp.OCSPCertStatus.GOOD else None
    except Exception:
        return None


def check_path_revocation(
    path: list[x509.Certificate],
    *,
    crls: Iterable[x509.CertificateRevocationList] = (),
    ocsp_responses: Iterable[ocsp.OCSPResponse] = (),
    prefix: str,
    now: dt.datetime,
    required: str = "all",
    allow_missing: bool = False,
) -> None:
    """Refuse a verified ``path`` (leaf ... pinned root) that contains a revoked certificate or a
    link with no current revocation statement. ``allow_missing`` only tolerates the missing
    statement (never a revoked one); production callers do not set it."""
    crls, ocsp_responses = list(crls), list(ocsp_responses)
    links = list(zip(path[:-1], path[1:]))
    for index, (cert, issuer) in enumerate(links):
        statuses = [s for s in (_crl_status(c, cert, issuer, now) for c in crls) if s]
        statuses += [s for s in (_ocsp_status(r, cert, issuer, now) for r in ocsp_responses) if s]
        name = cert.subject.rfc4514_string()
        if "revoked" in statuses:
            raise HardwareAttestationError(f"{prefix}_certificate_revoked", f"certificate {name} is revoked by its issuer")
        if statuses:
            continue
        if allow_missing or (required == "leaf" and index > 0):
            continue
        raise HardwareAttestationError(
            f"{prefix}_revocation_unknown",
            f"no current, issuer-signed CRL or OCSP response covers {name}",
        )
