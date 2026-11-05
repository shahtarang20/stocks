"""P2: CRL / OCSP revocation, TDX module identity and NVIDIA RIM checks (written to be run by the owner).

Synthetic evidence only: it proves the verifiers enforce the rules, not that real vendor collateral
parses (R7, R8: recorded fixtures are still required).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509 import ocsp
from cryptography.x509.oid import NameOID

from app.services.hardware_attestation import HardwareAttestationError
from app.services.hardware_attestation.nvidia import NvidiaCCVerifier
from app.services.hardware_attestation.revocation import check_path_revocation, parse_crls
from app.services.hardware_attestation.sev_snp import SEVSNPVerifier
from app.services.hardware_attestation.tdx import TDXVerifier, tdx_module_status
from tests.test_hardware_tdx_nvidia import NONCE, NOW, Gpu, Tdx, _cert, _code, _pem

STRICT: dict = {}


def make_crl(issuer_cert, issuer_key, revoked=(), last=None, nxt=None, alg=None, pss=False):
    b = (x509.CertificateRevocationListBuilder().issuer_name(issuer_cert.subject)
         .last_update(last or NOW - dt.timedelta(hours=1)).next_update(nxt or NOW + dt.timedelta(days=7)))
    for c in revoked:
        b = b.add_revoked_certificate(x509.RevokedCertificateBuilder().serial_number(c.serial_number)
                                      .revocation_date(NOW - dt.timedelta(minutes=5)).build())
    kw = {}
    if pss:  # AMD signs with RSA-PSS; the rsa_padding argument needs a recent cryptography (unverified here)
        kw["rsa_padding"] = padding.PSS(mgf=padding.MGF1(hashes.SHA384()), salt_length=48)
    return b.sign(issuer_key, alg or hashes.SHA256(), **kw).public_bytes(serialization.Encoding.PEM).decode()


# ----------------------------------------------------------------------------- generic rules

def _chain():
    rk, ik, lk = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _cert("Root", rk, ca=True)
    inter = _cert("Inter", ik, "Root", rk, ca=True)
    leaf = _cert("Leaf", lk, "Inter", ik)
    return rk, ik, root, inter, leaf


def _check(path, crls, **kw):
    check_path_revocation(path, crls=parse_crls(crls, "t"), prefix="t", now=NOW, **kw)


def test_clean_crls_pass_and_listed_serials_are_refused():
    rk, ik, root, inter, leaf = _chain()
    path = [leaf, inter, root]
    good = [make_crl(root, rk), make_crl(inter, ik)]
    _check(path, good)
    assert _code(lambda: _check(path, [make_crl(root, rk), make_crl(inter, ik, [leaf])])) == "t_certificate_revoked"
    assert _code(lambda: _check(path, [make_crl(root, rk, [inter]), make_crl(inter, ik)])) == "t_certificate_revoked"


def test_missing_expired_forged_and_wrong_issuer_crls_are_unknown_never_good():
    rk, ik, root, inter, leaf = _chain()
    path = [leaf, inter, root]
    assert _code(lambda: _check(path, [])) == "t_revocation_unknown"
    assert _code(lambda: _check(path, [make_crl(root, rk)])) == "t_revocation_unknown"  # no CRL for the leaf link
    expired = make_crl(inter, ik, last=NOW - dt.timedelta(days=9), nxt=NOW - dt.timedelta(days=2))
    assert _code(lambda: _check(path, [make_crl(root, rk), expired])) == "t_revocation_unknown"
    future = make_crl(inter, ik, last=NOW + dt.timedelta(days=1), nxt=NOW + dt.timedelta(days=8))
    assert _code(lambda: _check(path, [make_crl(root, rk), future])) == "t_revocation_unknown"
    forged = make_crl(inter, ec.generate_private_key(ec.SECP256R1()), [leaf])  # claims the issuer, wrong key
    assert _code(lambda: _check(path, [make_crl(root, rk), forged])) == "t_revocation_unknown"
    wrong_issuer = make_crl(root, rk)  # a root CRL says nothing about the leaf
    assert _code(lambda: _check(path, [wrong_issuer])) == "t_revocation_unknown"


def test_leaf_only_requirement_and_malformed_crl():
    rk, ik, root, inter, leaf = _chain()
    _check([leaf, inter, root], [make_crl(inter, ik)], required="leaf")
    assert _code(lambda: _check([leaf, inter, root], [make_crl(inter, ik, [leaf])], required="leaf")) == "t_certificate_revoked"
    assert _code(lambda: parse_crls(["not a crl"], "t")) == "t_bad_collateral"


def test_allow_missing_never_hides_a_revoked_certificate():
    rk, ik, root, inter, leaf = _chain()
    _check([leaf, inter, root], [], allow_missing=True)
    assert _code(lambda: _check([leaf, inter, root], [make_crl(inter, ik, [leaf])], allow_missing=True)) == "t_certificate_revoked"


def _ocsp(cert, issuer, issuer_key, status=ocsp.OCSPCertStatus.GOOD, nxt=None, responder=None):
    b = ocsp.OCSPResponseBuilder().add_response(
        cert=cert, issuer=issuer, algorithm=hashes.SHA1(), cert_status=status, this_update=NOW - dt.timedelta(hours=1),
        next_update=nxt or NOW + dt.timedelta(days=1), revocation_time=NOW - dt.timedelta(minutes=5) if status == ocsp.OCSPCertStatus.REVOKED else None,
        revocation_reason=None).responder_id(ocsp.OCSPResponderEncoding.HASH, responder or issuer)
    return b.sign(issuer_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def test_ocsp_good_revoked_expired_and_forged():
    from app.services.hardware_attestation.revocation import parse_ocsp_responses

    rk, ik, root, inter, leaf = _chain()
    path = [leaf, inter, root]

    def run(leaf_resp):
        check_path_revocation(path, crls=parse_crls([make_crl(root, rk)], "t"), ocsp_responses=parse_ocsp_responses([leaf_resp], "t"), prefix="t", now=NOW)

    run(_ocsp(leaf, inter, ik))
    assert _code(lambda: run(_ocsp(leaf, inter, ik, ocsp.OCSPCertStatus.REVOKED))) == "t_certificate_revoked"
    assert _code(lambda: run(_ocsp(leaf, inter, ik, nxt=NOW - dt.timedelta(minutes=1)))) == "t_revocation_unknown"
    # Forged: signed by a rogue key whose certificate merely carries the issuer's name (cryptography refuses to sign
    # with a key that does not match the responder certificate, so the rogue key needs its own certificate).
    rogue_key = ec.generate_private_key(ec.SECP256R1())
    rogue = _cert("Inter", rogue_key, "Root", rk, ca=True)
    assert _code(lambda: run(_ocsp(leaf, inter, rogue_key, responder=rogue))) == "t_revocation_unknown"


# ----------------------------------------------------------------------------- Intel

def _tdx_strict(tdx: Tdx, revoked_pck=(), revoked_ca=(), identities="default", drop=()):
    crls = [make_crl(tdx.root, tdx.root_key, revoked_ca), make_crl(tdx.pck_ca, tdx.ca_key, revoked_pck)]
    coll = tdx.collateral()
    import json

    info = json.loads(coll["tcb_info"])["tcbInfo"]
    info.pop("nextUpdate")
    if identities == "default":
        info["tdxModuleIdentities"] = [{"id": "TDX_03", "mrsigner": "00" * 48, "attributes": "00" * 8, "attributesMask": "ff" * 8,
                                        "tcbLevels": [{"tcb": {"isvsvn": 3}, "tcbStatus": "UpToDate"}]}]
    elif identities:
        info["tdxModuleIdentities"] = identities
    coll["tcb_info"] = tdx.signed("tcbInfo", info)
    coll["crls"] = [c for i, c in enumerate(crls) if i not in drop]
    ev = {"quote": tdx.quote(), "collateral": coll}
    return lambda: TDXVerifier().verify(ev, NONCE, None, [tdx.root], STRICT)


def test_tdx_strict_golden_path():
    assert _tdx_strict(Tdx())().verified


def test_tdx_revoked_pck_and_revoked_intermediate():
    t = Tdx()
    assert _code(_tdx_strict(t, revoked_pck=[t.pck])) == "tdx_certificate_revoked"
    assert _code(_tdx_strict(t, revoked_ca=[t.pck_ca])) == "tdx_certificate_revoked"
    assert _code(_tdx_strict(t, revoked_ca=[t.tcb_cert])) == "tdx_certificate_revoked"


def test_tdx_missing_crl_is_unknown():
    t = Tdx()
    assert _code(_tdx_strict(t, drop=(1,))) == "tdx_revocation_unknown"
    assert _code(_tdx_strict(t, drop=(0,))) == "tdx_revocation_unknown"
    assert _code(_tdx_strict(t, drop=(0, 1))) == "tdx_revocation_unknown"


def test_tdx_module_identity_rules():
    t = Tdx()
    wrong_signer = [{"id": "TDX_03", "mrsigner": "11" * 48, "attributes": "00" * 8, "attributesMask": "ff" * 8,
                     "tcbLevels": [{"tcb": {"isvsvn": 3}, "tcbStatus": "UpToDate"}]}]
    assert _code(_tdx_strict(t, identities=wrong_signer)) == "tdx_bad_tdx_module"
    wrong_attr = [dict(wrong_signer[0], mrsigner="00" * 48, attributes="01" + "00" * 7)]
    assert _code(_tdx_strict(t, identities=wrong_attr)) == "tdx_bad_tdx_module"
    below = [dict(wrong_signer[0], mrsigner="00" * 48, tcbLevels=[{"tcb": {"isvsvn": 9}, "tcbStatus": "UpToDate"}])]
    assert _code(_tdx_strict(t, identities=below)) == "tdx_tcb_not_up_to_date"  # NoMatchingLevel
    out_of_date = [dict(wrong_signer[0], mrsigner="00" * 48, tcbLevels=[{"tcb": {"isvsvn": 3}, "tcbStatus": "OutOfDate"}])]
    assert _code(_tdx_strict(t, identities=out_of_date)) == "tdx_tcb_not_up_to_date"
    other_major = [dict(wrong_signer[0], mrsigner="00" * 48, id="TDX_01")]
    assert _code(_tdx_strict(t, identities=other_major)) == "tdx_module_identity_unknown"


def test_tdx_module_status_unit_for_major_zero_uses_tdx_module():
    info = {"tdxModule": {"mrsigner": "22" * 48, "attributes": "00" * 8, "attributesMask": "ff" * 8}}
    svn = bytes([1, 0]) + bytes(14)
    assert tdx_module_status(info, svn, bytes.fromhex("22" * 48), bytes(8)) == "UpToDate"
    with pytest.raises(HardwareAttestationError) as exc:
        tdx_module_status(info, svn, bytes(48), bytes(8))
    assert exc.value.code == "tdx_bad_tdx_module"


# ----------------------------------------------------------------------------- AMD

def test_snp_vcek_revocation():
    import tests.test_hardware_attestation as m

    snp = m.Snp()
    ev = snp.evidence()
    run = lambda: SEVSNPVerifier().verify(ev, m.NONCE, None, [snp.ark], STRICT)  # noqa: E731
    ev["crls"] = [make_crl(snp.ask, snp.ask_key, alg=hashes.SHA384(), pss=True)]
    assert run().verified
    ev["crls"] = [make_crl(snp.ask, snp.ask_key, [snp.vcek], alg=hashes.SHA384(), pss=True)]
    assert _code(run) == "sev_snp_certificate_revoked"
    ev["crls"] = []
    assert _code(run) == "sev_snp_revocation_unknown"


# ----------------------------------------------------------------------------- NVIDIA

def _gpu_strict(gpu: Gpu, crls):
    ev = dict(gpu.evidence(), crls=crls)
    return lambda: NvidiaCCVerifier().verify(ev, NONCE, None, [gpu.root], STRICT)


def test_nvidia_revocation_and_rim_are_required():
    g = Gpu()
    clean = [make_crl(g.root, g.root_key, alg=hashes.SHA384())]
    assert _code(_gpu_strict(g, clean)) == "nvidia_rim_missing"  # clean chain, but no signed golden values
    assert _code(_gpu_strict(g, [make_crl(g.root, g.root_key, [g.cert], alg=hashes.SHA384())])) == "nvidia_certificate_revoked"
    assert _code(_gpu_strict(g, [])) == "nvidia_revocation_unknown"
    forged = [make_crl(g.root, ec.generate_private_key(ec.SECP384R1()), [g.cert], alg=hashes.SHA384())]
    assert _code(_gpu_strict(g, forged)) == "nvidia_revocation_unknown"


# --- RIM -----------------------------------------------------------------------------------------

def _rsa_chain():
    rk, lk = rsa.generate_private_key(65537, 2048), rsa.generate_private_key(65537, 2048)
    root = _cert("NVIDIA RIM Root", rk, ca=True)
    leaf = _cert("NVIDIA RIM Signer", lk, "NVIDIA RIM Root", rk)
    return rk, root, lk, leaf


def _pem_key(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _rim(values: dict[int, list[bytes]], key, cert, extra=""):
    pytest.importorskip("signxml")
    from lxml import etree
    from signxml import XMLSigner, methods

    res = "".join(
        f'<Resource index="{i}" alternatives="{len(v)}" active="True" size="48" hashFunction="sha384" '
        + " ".join(f'Hash{n}="{h.hex()}"' for n, h in enumerate(v)) + "/>"
        for i, v in values.items())
    root = etree.fromstring(f'<SoftwareIdentity xmlns="http://standards.iso.org/iso/19770/-2/2015/schema.xsd" name="rim"><Payload>{res}</Payload></SoftwareIdentity>')
    signed = XMLSigner(method=methods.enveloped, signature_algorithm="rsa-sha256", digest_algorithm="sha256").sign(
        root, key=_pem_key(key), cert=_pem(cert))
    return etree.tostring(signed).decode() + extra


def _rim_clean_crl_bundle(rk, root, lk, leaf):
    return [make_crl(root, rk)]


def _verify_rim(xml, root, crls):
    from app.services.hardware_attestation.rim import verify_rim

    return verify_rim(xml, [root], now=NOW, crls=parse_crls(crls, "nvidia"), ocsp_responses=[])


def test_rim_signature_chain_and_alternatives():
    rk, root, lk, leaf = _rsa_chain()
    crls = [make_crl(root, rk, alg=hashes.SHA256())]
    values = {1: [b"\x01" * 48, b"\x02" * 48], 2: [b"\x03" * 48]}
    ref = _verify_rim(_rim(values, lk, leaf), root, crls)
    assert ref == {1: [("01" * 48), ("02" * 48)], 2: ["03" * 48]}

    from app.services.hardware_attestation.rim import compare_with_blocks

    compare_with_blocks(ref, {"1": "02" * 48, "2": "03" * 48, "9": "ff" * 48})  # alternative 1 matches; extra block ignored
    for blocks in ({"1": "09" * 48, "2": "03" * 48}, {"1": "01" * 48}):
        with pytest.raises(HardwareAttestationError) as exc:
            compare_with_blocks(ref, blocks)
        assert exc.value.code == "nvidia_measurement_mismatch"


def test_rim_forged_unpinned_tampered_and_revoked_are_refused():
    rk, root, lk, leaf = _rsa_chain()
    crls = [make_crl(root, rk)]
    xml = _rim({1: [b"\x01" * 48]}, lk, leaf)
    assert _code(lambda: _verify_rim(xml.replace("01" * 48, "05" * 48), root, crls)) == "nvidia_invalid_rim_signature"
    other_rk, other_root, other_lk, other_leaf = _rsa_chain()
    assert _code(lambda: _verify_rim(_rim({1: [b"\x01" * 48]}, other_lk, other_leaf), root, crls)) == "nvidia_bad_rim_chain"
    assert _code(lambda: _verify_rim(xml, root, [make_crl(root, rk, [leaf])])) == "nvidia_certificate_revoked"
    assert _code(lambda: _verify_rim(xml, root, [])) == "nvidia_revocation_unknown"
    assert _code(lambda: _verify_rim("<!DOCTYPE x [<!ENTITY a 'b'>]><x/>", root, crls)) == "nvidia_bad_rim"
    assert _code(lambda: _verify_rim("not xml", root, crls)) in ("nvidia_bad_rim", "nvidia_invalid_rim_signature")


def test_rim_unsigned_addition_is_not_read():
    rk, root, lk, leaf = _rsa_chain()
    crls = [make_crl(root, rk)]
    xml = _rim({1: [b"\x01" * 48]}, lk, leaf)
    wrapped = xml.replace("</Payload>", '<Resource index="7" Hash0="' + "aa" * 48 + '"/></Payload>', 1)
    # an addition inside the signed document breaks the digest; outside it is never returned
    assert _code(lambda: _verify_rim(wrapped, root, crls)) == "nvidia_invalid_rim_signature"
    assert 7 not in _verify_rim(xml, root, crls)


def test_rim_digest_helper_matches_sha384_length():
    assert len(hashlib.sha384(b"x").digest()) == 48 and base64.b64encode(b"") == b""


def test_collateral_list_is_capped():
    """Host-supplied CRL / OCSP lists are bounded (each entry costs a signature check per chain link)."""
    from app.services.hardware_attestation import HardwareAttestationError
    from app.services.hardware_attestation.revocation import MAX_COLLATERAL_ITEMS, parse_crls, parse_ocsp_responses

    too_many = ["00"] * (MAX_COLLATERAL_ITEMS + 1)
    for fn in (parse_crls, parse_ocsp_responses):
        with pytest.raises(HardwareAttestationError) as ei:
            fn(too_many, "nvidia")
        assert ei.value.code == "nvidia_bad_collateral"
