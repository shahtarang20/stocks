"""NVIDIA Reference Integrity Manifest (RIM) verification (P2 / R5).

A RIM is a SWID (ISO/IEC 19770-2) XML document, signed with an enveloped XML signature whose
``KeyInfo/X509Data`` carries the signing chain. The golden GPU measurements come from RIMs, not
from configuration. A RIM is accepted only when:

1. its XML signature verifies (``signxml``, pinned version) with the leaf certificate it carries;
2. that leaf chains, link by link, to a PINNED NVIDIA root (``PRIVACY_HARDWARE_ROOT_CERTS``) and no
   certificate of the chain is revoked (revocation.py);
3. only the data covered by the signature is read (the element ``signxml`` returns, so a document
   with unsigned additions or a wrapped signature yields nothing).

``Resource`` elements under ``Payload`` define ``index``, the number of ``alternatives`` and
``Hash0 .. HashN``; any listed alternative may match the device's SPDM measurement block.

Format notes recorded per R7 (UNVERIFIED against current NVIDIA docs / a real RIM in this change;
the attribute names ``index``, ``alternatives``, ``active``, ``Hash<N>`` follow NVIDIA's nvtrust RIM
handling as remembered): re-check, then add real RIM fixtures with SHA-256 and source URL.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from app.services.hardware_attestation import HardwareAttestationError, verify_chain
from app.services.hardware_attestation.revocation import check_path_revocation

DS = "http://www.w3.org/2000/09/xmldsig#"
MAX_RIM_BYTES = 1_000_000


def _err(code: str, detail: str) -> HardwareAttestationError:
    return HardwareAttestationError(f"nvidia_{code}", detail)


def _parser():
    from lxml import etree

    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False)


def _embedded_certs(xml: bytes) -> list[x509.Certificate]:
    from lxml import etree

    try:
        root = etree.fromstring(xml, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise _err("bad_rim", "RIM is not well-formed XML") from exc
    certs = []
    for el in root.iter(f"{{{DS}}}X509Certificate"):
        try:
            certs.append(x509.load_der_x509_certificate(base64.b64decode("".join((el.text or "").split()), validate=True)))
        except (ValueError, binascii.Error) as exc:
            raise _err("bad_rim", "RIM carries an unreadable certificate") from exc
    if not certs:
        raise _err("bad_rim", "RIM carries no signing certificate")
    return certs


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def verify_rim(
    xml_text: Any,
    root_certs: list,
    *,
    now: dt.datetime,
    crls: list,
    ocsp_responses: list,
    allow_missing_revocation: bool = False,
) -> dict[int, list[str]]:
    """Verify one RIM and return ``{measurement index: [accepted hex values]}`` from its signed bytes."""
    if not isinstance(xml_text, str) or not xml_text.strip() or len(xml_text) > MAX_RIM_BYTES:
        raise _err("bad_rim", "RIM must be non-empty XML text")
    xml = xml_text.encode("utf-8")
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise _err("bad_rim", "RIM must not carry a DTD")
    try:
        from signxml import XMLVerifier
        from signxml.exceptions import InvalidInput, InvalidSignature
    except ImportError as exc:  # fail closed: no library, no RIM
        raise _err("rim_unsupported", "signxml is not installed, so RIMs cannot be verified") from exc

    certs = _embedded_certs(xml)
    leaf = next((c for c in certs if not _is_ca(c)), certs[0])
    path = verify_chain(leaf, [c for c in certs if c is not leaf], root_certs, code="nvidia_bad_rim_chain", now=now)
    check_path_revocation(path, crls=crls, ocsp_responses=ocsp_responses, prefix="nvidia", now=now, allow_missing=allow_missing_revocation)
    try:
        result = XMLVerifier().verify(
            xml, x509_cert=leaf.public_bytes(Encoding.PEM).decode("ascii"), parser=_parser(), expect_references=1
        )
    except (InvalidSignature, InvalidInput, ValueError, TypeError) as exc:
        raise _err("invalid_rim_signature", "RIM signature does not verify") from exc
    except Exception as exc:  # any library failure is a refusal
        raise _err("invalid_rim_signature", "RIM signature could not be verified") from exc
    return parse_signed_rim(result.signed_xml)


def parse_signed_rim(signed: Any) -> dict[int, list[str]]:
    """Resource -> index -> alternatives, from the element the signature covers."""
    wanted: dict[int, list[str]] = {}
    for el in signed.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag.rsplit("}", 1)[-1] != "Resource":
            continue
        attrs = {k.rsplit("}", 1)[-1]: v for k, v in el.attrib.items()}
        if str(attrs.get("active", "True")).lower() == "false" or "index" not in attrs:
            continue
        try:
            index = int(attrs["index"])
        except ValueError as exc:
            raise _err("bad_rim", "Resource index is not a number") from exc
        hashes_ = []
        for k, v in attrs.items():
            if k.startswith("Hash") and k[4:].isdigit():
                value = v.strip().lower()
                try:
                    bytes.fromhex(value)
                except ValueError as exc:
                    raise _err("bad_rim", f"{k} of measurement {index} is not hex") from exc
                hashes_.append(value)
        declared = attrs.get("alternatives")
        if not hashes_ or (declared is not None and str(declared).isdigit() and int(declared) != len(hashes_)):
            raise _err("bad_rim", f"measurement {index} lists no hash or a wrong number of alternatives")
        if index in wanted:
            raise _err("bad_rim", f"measurement {index} is defined twice")
        wanted[index] = hashes_
    if not wanted:
        raise _err("bad_rim", "RIM defines no measurement")
    return wanted


def compare_with_blocks(reference: dict[int, list[str]], measurements: dict[str, str]) -> None:
    """Every index the RIM defines must be present in the signed SPDM blocks and equal an alternative."""
    for index, alternatives in sorted(reference.items()):
        got = measurements.get(str(index))
        if got is None:
            raise _err("measurement_mismatch", f"RIM defines measurement {index} but the device did not report it")
        if got.lower() not in alternatives:
            raise _err("measurement_mismatch", f"measurement {index} matches no value in the signed RIM")
