"""NVIDIA attestation adapter (nvidia_eat.py) and its use by NvidiaGpuCollector. WRITTEN, NOT YET RUN.

Fixtures are AUTHORED here from the documented concepts (evidence list; detached EAT / EAR results); none is a capture
from a real tool. TODO(verify-docs): replace them with captures from a Confidential Computing GPU.

Bug re-insertion: accept a result in normalize_tool_output (the *_result_is_refused tests fail); skip the nonce check in
_from_evidence_entry (test_evidence_entry_nonce_must_match fails); drop the 37-byte split (the split tests fail).
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess

import pytest

from computecoin_agent.nvidia_eat import (
    MAX_CHAIN_CERTS,
    MAX_OUTPUT_BYTES,
    NvidiaEatError,
    NvidiaResultError,
    looks_like_result,
    normalize_tool_output,
    pem_blocks,
    summarize_result,
)
from computecoin_agent.tee import NvidiaGpuCollector, TeeError

CHALLENGE = "challenge-123"
NONCE = hashlib.sha256(CHALLENGE.encode()).digest()
PEM_A = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
PEM_B = "-----BEGIN CERTIFICATE-----\nBBBB\n-----END CERTIFICATE-----\n"


def request(nonce=NONCE, code=0xE0):
    return bytes([0x11, code, 1, 0xFF]) + nonce + b"\x00"


def response():
    return bytes([0x11, 0x60]) + bytes(40)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def jwt(payload: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'ES384', 'typ': 'JWT'})}.{seg(payload)}.c2lnbmF0dXJl"


def contract_doc():
    return {"request": request().hex(), "response": response().hex(), "certificate_chain": [PEM_A, PEM_B]}


def evidence_entry(**kw):
    entry = {"arch": "HOPPER", "evidence": b64(request() + response()), "certificate": b64((PEM_A + PEM_B).encode())}
    entry.update(kw)
    return entry


def out(doc) -> bytes:
    return json.dumps(doc).encode()


# ---- authored result fixtures (documented concepts) -------------------------------------------------------------
def detached_eat():
    overall = jwt({"iss": "https://nras.attestation.nvidia.com", "eat_nonce": NONCE.hex(), "x-nvidia-overall-att-result": True,
                   "x-nvidia-ver": "2.0"})
    gpu = jwt({"eat_nonce": NONCE.hex(), "x-nvidia-gpu-driver-version": "550.54", "secboot": True, "dbgstat": "disabled"})
    return [["JWT", overall], {"LOCAL_GPU_CLAIMS": ["JWT", gpu]}]


def ear_result():
    return {"eat_profile": "tag:github.com,2023:veraison/ear", "eat_nonce": NONCE.hex(),
            "submods": {"GPU-0": {"ear.status": "affirming", "ear.trustworthiness-vector": {"hardware": 2}}}}


# ---- accepted forms ----------------------------------------------------------------------------------------------
def test_contract_form_passes_through_with_collateral():
    doc = contract_doc() | {"crls": ["c1"], "ocsp_responses": ["o1"], "rims": ["r1", "r2"], "ignored": 1}
    ev = normalize_tool_output(out(doc), nonce=NONCE)
    assert (ev.request, ev.response, ev.source) == (request(), response(), "contract")
    assert ev.certificate_chain == [PEM_A, PEM_B]
    assert ev.collateral == {"crls": ["c1"], "ocsp_responses": ["o1"], "rims": ["r1", "r2"]}


def test_evidence_list_is_split_into_request_and_response():
    ev = normalize_tool_output(out([evidence_entry()]), nonce=NONCE)
    assert ev.source == "evidence_list"
    assert ev.request == request() and len(ev.request) == 37
    assert ev.response == response()
    assert ev.certificate_chain == [PEM_A.strip() + "\n", PEM_B.strip() + "\n"]


def test_evidence_list_accepts_wrapped_bare_and_pem_text_certificates():
    for doc in ({"evidence_list": [evidence_entry()]}, {"evidences": [evidence_entry()]}, evidence_entry(),
                [evidence_entry(certificate=PEM_A + PEM_B)], [evidence_entry(certificate=None, certificate_chain=[PEM_A, PEM_B])]):
        ev = normalize_tool_output(out(doc), nonce=NONCE)
        assert ev.request == request() and len(ev.certificate_chain) == 2, doc


def test_evidence_list_selects_the_entry_for_the_gpu_index():
    other = evidence_entry(evidence=b64(request() + bytes([0x11, 0x60]) + b"\x07" * 40))
    ev = normalize_tool_output(out([evidence_entry(), other]), nonce=NONCE, gpu_index=1)
    assert ev.response.endswith(b"\x07" * 40)
    with pytest.raises(NvidiaEatError, match="no entry for GPU index 2"):
        normalize_tool_output(out([evidence_entry(), other]), nonce=NONCE, gpu_index=2)
    with pytest.raises(NvidiaEatError):
        normalize_tool_output(out([evidence_entry()]), nonce=NONCE, gpu_index=-1)


def test_evidence_entry_nonce_must_match():
    ok = normalize_tool_output(out([evidence_entry(nonce=NONCE.hex())]), nonce=NONCE)
    assert ok.request == request()
    for bad in (hashlib.sha256(b"other").hexdigest(), "zz", 5):
        with pytest.raises(NvidiaEatError):
            normalize_tool_output(out([evidence_entry(nonce=bad)]), nonce=NONCE)


# ---- refused forms: results ------------------------------------------------------------------------------------
@pytest.mark.parametrize("doc", [detached_eat(), ear_result(), {"eat_nonce": "ab", "x-nvidia-overall-att-result": True}])
def test_result_is_refused_with_a_clear_error(doc):
    with pytest.raises(NvidiaResultError) as exc:
        normalize_tool_output(out(doc), nonce=NONCE)
    text = str(exc.value)
    assert "attestation RESULT" in text and "no raw SPDM evidence" in text and "nvidia-attestation-wrapper.md" in text


def test_bare_jwt_on_stdout_is_refused_as_a_result():
    token = detached_eat()[0][1]
    with pytest.raises(NvidiaResultError):
        normalize_tool_output(token.encode() + b"\n", nonce=NONCE)


def test_a_result_that_says_pass_is_still_refused():
    # A host verdict is not evidence: even "x-nvidia-overall-att-result": true must not turn into verifier input.
    assert summarize_result(detached_eat()).overall_result is True
    with pytest.raises(NvidiaResultError, match="host-side verdict: pass"):
        normalize_tool_output(out(detached_eat()), nonce=NONCE)


def test_result_summary_is_marked_untrusted_data_only():
    s = summarize_result(ear_result())
    assert s.overall_result is True and s.nonce == NONCE.hex() and "submods" in s.claims_seen
    assert looks_like_result(detached_eat()) and looks_like_result(ear_result())
    assert not looks_like_result(contract_doc()) and not looks_like_result([evidence_entry()])


# ---- refused forms: malformed ----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    b"", b"not json", b"[]", b"{}", b'"string"', b"123", b"null",
    json.dumps({"request": "zz", "response": "11", "certificate_chain": [PEM_A]}).encode(),
    json.dumps({"request": "11", "response": "11", "certificate_chain": []}).encode(),
    json.dumps({"request": "11", "response": "11", "certificate_chain": "PEM"}).encode(),
    json.dumps([{"evidence": "!!!notbase64", "certificate": b64(PEM_A.encode())}]).encode(),
    json.dumps([{"evidence": b64(b"short"), "certificate": b64(PEM_A.encode())}]).encode(),
    json.dumps([{"evidence": b64(request() + response())}]).encode(),
    json.dumps([{"evidence": b64(request() + response()), "certificate": b64(b"no pem here")}]).encode(),
])
def test_unusable_output_is_refused(raw):
    with pytest.raises(NvidiaEatError):
        normalize_tool_output(raw, nonce=NONCE)


def test_output_size_and_chain_length_are_capped():
    with pytest.raises(NvidiaEatError):
        normalize_tool_output(b" " * (MAX_OUTPUT_BYTES + 1), nonce=NONCE)
    many = "".join(PEM_A for _ in range(MAX_CHAIN_CERTS + 1))
    with pytest.raises(NvidiaEatError):
        pem_blocks(many)
    with pytest.raises(NvidiaEatError):
        normalize_tool_output(out([evidence_entry(certificate=many)]), nonce=NONCE)
    with pytest.raises(NvidiaEatError):
        normalize_tool_output(out([evidence_entry()] * 65), nonce=NONCE)


# ---- through the collector (the agent's fail-closed checks still apply) ----------------------------------------
def _runner(stdout: bytes, rc: int = 0, calls=None):
    def run(args, **kw):
        if calls is not None:
            calls.append(args)
        return subprocess.CompletedProcess(args, rc, stdout, b"boom")

    return run


def test_collector_accepts_the_evidence_list_form():
    calls = []
    ev = NvidiaGpuCollector(["nvtool"], runner=_runner(out([evidence_entry()]), calls=calls)).collect(CHALLENGE)
    assert calls[0][calls[0].index("--nonce") + 1] == NONCE.hex()
    assert ev["request"] == request().hex() and ev["response"] == response().hex()
    assert len(ev["certificate_chain"]) == 2


def test_collector_still_accepts_the_contract_form():
    ev = NvidiaGpuCollector(["nvtool"], runner=_runner(out(contract_doc()))).collect(CHALLENGE)
    assert set(ev) == {"request", "response", "certificate_chain"}


def test_collector_refuses_a_result_with_the_adapter_message():
    with pytest.raises(TeeError, match="attestation RESULT"):
        NvidiaGpuCollector(["nvtool"], runner=_runner(out(detached_eat()))).collect(CHALLENGE)


def test_collector_refuses_evidence_for_another_challenge_even_without_an_entry_nonce():
    stale = evidence_entry(evidence=b64(request(nonce=hashlib.sha256(b"old").digest()) + response()))
    with pytest.raises(TeeError, match="not bound to this challenge"):
        NvidiaGpuCollector(["nvtool"], runner=_runner(out([stale]))).collect(CHALLENGE)


def test_collector_refuses_a_wrong_layout_where_the_prefix_is_not_a_signed_get_measurements():
    # If the evidence blob were NOT request(37)||response, the 37-byte prefix would not parse as one.
    wrong = evidence_entry(evidence=b64(bytes(60) + response()))
    with pytest.raises(TeeError):
        NvidiaGpuCollector(["nvtool"], runner=_runner(out([wrong]))).collect(CHALLENGE)


def test_collector_passes_its_gpu_index_to_the_adapter():
    second = evidence_entry(evidence=b64(request() + bytes([0x11, 0x60]) + b"\x09" * 40))
    ev = NvidiaGpuCollector(["nvtool"], gpu_index=1, runner=_runner(out([evidence_entry(), second]))).collect(CHALLENGE)
    assert ev["response"].endswith("09" * 40)
