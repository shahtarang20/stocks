"""P4 round trip: evidence built by the agent's collectors is accepted by the API's real verifiers,
and the agent's gpu_bound_nonce is identical to the API's. Synthetic hardware comes from the API's
own verifier tests (loaded by path)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes

from app.services.attestation_verifier import gpu_bound_nonce as api_gpu_bound_nonce
from app.services.hardware_attestation.nvidia import NvidiaCCVerifier
from app.services.hardware_attestation.sev_snp import SEVSNPVerifier
from app.services.hardware_attestation.tdx import TDXVerifier
from computecoin_agent.tee import NvidiaGpuCollector, SevSnpCollector, TdxCollector, confidential_provider, gpu_bound_nonce

API_TESTS = Path(__file__).resolve().parents[2] / "apps/api/tests"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_api_{name}", API_TESTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NONCE = "roundtrip-challenge"


@pytest.mark.parametrize("response", ["", "ABCdef01", "00" * 200])
def test_gpu_bound_nonce_matches_api(response):
    ev = {"response": response}
    assert gpu_bound_nonce("abc", ev) == api_gpu_bound_nonce("abc", ev)


def _gpu_runner(gpu, collateral):
    def run(args, **kw):
        assert bytes.fromhex(args[args.index("--nonce") + 1]) == hashlib.sha256(NONCE.encode()).digest()
        return subprocess.CompletedProcess(args, 0, json.dumps({**gpu.evidence(nonce=NONCE), **collateral}).encode(), b"")
    return run


@pytest.mark.parametrize("flavor", ["sev_snp", "tdx"])
def test_agent_evidence_is_accepted_by_api_verifiers(flavor):
    tdx_mod, snp_mod, p2 = _load("test_hardware_tdx_nvidia"), _load("test_hardware_attestation"), _load("test_hardware_revocation_rim_p2")
    gpu = tdx_mod.Gpu()
    # R1/R5: the verifiers are called with NO tolerance for missing collateral, so the agent has to carry
    # the issuers' CRLs (and NVIDIA's signed RIM) in the evidence, exactly as production evidence must.
    gpu_collateral = {"crls": [p2.make_crl(gpu.root, gpu.root_key, alg=hashes.SHA384())]}
    gpu_roots, gpu_ctx = [gpu.root], {}
    try:
        rim_key, rim_root, rim_leaf_key, rim_leaf = p2._rsa_chain()
        gpu_collateral["rims"] = [p2._rim({i: [v] for i, v in gpu.values.items()}, rim_leaf_key, rim_leaf)]
        gpu_collateral["crls"].append(p2.make_crl(rim_root, rim_key, alg=hashes.SHA256()))
        gpu_roots.append(rim_root)
    except (Exception, pytest.skip.Exception):  # signxml missing / incompatible with the installed cryptography (declared dependency)
        gpu_collateral.pop("rims", None)
        gpu_ctx = {"legacy_collateral_ok": True}  # tolerates only the MISSING RIM here; the CRLs are still enforced
    gpu_collector = NvidiaGpuCollector(["nvtool"], runner=_gpu_runner(gpu, gpu_collateral))
    if flavor == "sev_snp":
        snp = snp_mod.Snp()

        def report_fn(report_data):  # the "guest": signs a report over the requested REPORT_DATA
            r = bytearray(snp.report(nonce="x"))
            r[0x50:0x90] = report_data
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
            rr, ss = decode_dss_signature(snp.vcek_key.sign(bytes(r[:0x2A0]), ec.ECDSA(hashes.SHA384())))
            r[0x2A0:0x2A0 + 72] = rr.to_bytes(72, "little")
            r[0x2A0 + 72:0x2A0 + 144] = ss.to_bytes(72, "little")
            return bytes(r)

        with tempfile.TemporaryDirectory() as d:
            Path(d, "vcek.pem").write_text(snp_mod._pem(snp.vcek))
            Path(d, "ask.pem").write_text(snp_mod._pem(snp.ask))
            Path(d, "crl.pem").write_text(p2.make_crl(snp.ask, snp.ask_key, alg=hashes.SHA384(), pss=True))
            cpu = SevSnpCollector(cache_dir=d, report_fn=report_fn)
            fl, ev = confidential_provider(cpu, gpu_collector)(NONCE)
        root, verifier = snp.ark, SEVSNPVerifier()
    else:
        tdx = tdx_mod.Tdx()

        def report_fn(report_data):
            q = bytearray(bytes.fromhex(tdx.quote(nonce="x")))
            # Re-sign is needed because REPORTDATA is signed: rebuild the quote with this report data.
            return bytes.fromhex(_quote_with(tdx, report_data))

        def _quote_with(tdx_, report_data):
            real = hashlib.sha512
            # The synthetic builder derives REPORTDATA from a nonce string; patch it for this call.
            import types
            fake = types.SimpleNamespace(sha512=lambda b: types.SimpleNamespace(digest=lambda: report_data), sha256=hashlib.sha256, sha384=hashlib.sha384)
            tdx_mod.hashlib = fake
            try:
                return tdx_.quote(nonce="x")
            finally:
                tdx_mod.hashlib = hashlib

        collateral = tdx.collateral()
        info = json.loads(collateral["tcb_info"])["tcbInfo"]
        info.pop("nextUpdate")
        info["tdxModuleIdentities"] = [{"id": "TDX_03", "mrsigner": "00" * 48, "attributes": "00" * 8, "attributesMask": "ff" * 8,
                                        "tcbLevels": [{"tcb": {"isvsvn": 3}, "tcbStatus": "UpToDate"}]}]
        collateral["tcb_info"] = tdx.signed("tcbInfo", info)
        with tempfile.TemporaryDirectory() as crl_dir:  # the agent relays the CRLs from its collateral cache
            Path(crl_dir, "crls.pem").write_text(
                p2.make_crl(tdx.root, tdx.root_key) + p2.make_crl(tdx.pck_ca, tdx.ca_key))
            cpu = TdxCollector(cache_dir=crl_dir, fetcher=lambda url: (collateral["qe_identity"].encode(), {}), report_fn=report_fn)
            cpu._collateral = lambda quote: dict(collateral)
            fl, ev = confidential_provider(cpu, gpu_collector)(NONCE)
        root, verifier = tdx.root, TDXVerifier()

    assert fl == "confidential" and ev["cpu_flavor"] == flavor
    if gpu_ctx:  # strict: revocation passed; the only thing still missing is the RIM
        with pytest.raises(Exception, match="Reference Integrity Manifest"):
            NvidiaCCVerifier().verify(ev["gpu"], NONCE, None, gpu_roots, {})
    NvidiaCCVerifier().verify(ev["gpu"], NONCE, None, gpu_roots, gpu_ctx)
    res = verifier.verify(ev["cpu"], api_gpu_bound_nonce(NONCE, ev["gpu"]), None, [root])  # strict: no context
    assert res.verified
    # CPU report is bound to the GPU evidence: it does not verify against the plain challenge.
    with pytest.raises(Exception):
        verifier.verify(ev["cpu"], NONCE, None, [root])
