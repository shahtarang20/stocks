"""Confidential-tier collectors (P4) with injected fake devices, runners and fetchers."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from computecoin_agent.attestation import AttestationClientError
from computecoin_agent.tee import (
    NvidiaGpuCollector,
    SevSnpCollector,
    TdxCollector,
    TeeError,
    confidential_provider,
    gpu_bound_nonce,
    hardware_for_tier,
    tsm_report,
)

NONCE = "n0nce"


def _gpu_json(nonce=NONCE, code=0xE0):
    req = bytes([0x11, code, 1, 0xFF]) + hashlib.sha256(nonce.encode()).digest() + b"\x00"
    resp = bytes([0x11, 0x60]) + bytes(40)
    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    return json.dumps({"request": req.hex(), "response": resp.hex(), "certificate_chain": [pem]}).encode()


def _runner(stdout=b"", rc=0, calls=None):
    def run(args, **kw):
        if calls is not None:
            calls.append(args)
        return subprocess.CompletedProcess(args, rc, stdout, b"boom")
    return run


def test_gpu_collector_passes_sha256_challenge_as_nonce():
    calls = []
    ev = NvidiaGpuCollector(["nvtool"], runner=_runner(_gpu_json(), calls=calls)).collect(NONCE)
    assert calls[0][calls[0].index("--nonce") + 1] == hashlib.sha256(NONCE.encode()).hexdigest()
    assert set(ev) == {"request", "response", "certificate_chain"}


@pytest.mark.parametrize("runner", [
    _runner(b"", rc=1), _runner(b"not json"), _runner(_gpu_json(nonce="other")), _runner(_gpu_json(code=0x84)),
])
def test_gpu_collector_failures_raise(runner):
    with pytest.raises(TeeError):
        NvidiaGpuCollector(["nvtool"], runner=runner).collect(NONCE)


def test_gpu_collector_needs_a_command():
    with pytest.raises(TeeError):
        NvidiaGpuCollector([])


def test_tee_error_is_an_attestation_error():
    assert issubclass(TeeError, AttestationClientError)


def test_snp_report_data_is_sha512_of_gpu_bound_nonce_and_failure_raises():
    seen = {}

    def report_fn(rd):
        seen["rd"] = rd
        raise TeeError("no /dev/sev-guest")

    gpu = NvidiaGpuCollector(["nvtool"], runner=_runner(_gpu_json()))
    cpu = SevSnpCollector(report_fn=report_fn)
    with pytest.raises(TeeError):
        confidential_provider(cpu, gpu)(NONCE)
    gpu_ev = gpu.collect(NONCE)
    assert seen["rd"] == hashlib.sha512(gpu_bound_nonce(NONCE, gpu_ev).encode()).digest()


def test_snp_rejects_wrong_size_and_unbound_report():
    with pytest.raises(TeeError):
        SevSnpCollector(report_fn=lambda rd: b"\x00" * 10).collect("x")
    with pytest.raises(TeeError):
        SevSnpCollector(report_fn=lambda rd: bytes(0x4A0)).collect("x")


def test_tdx_rejects_junk_quote():
    with pytest.raises(TeeError):
        TdxCollector(report_fn=lambda rd: b"\x00" * 700).collect("x")


def test_tsm_report_with_fake_configfs(tmp_path):
    root = tmp_path / "report"
    root.mkdir()

    class Fake:
        pass

    # A plain directory has no kernel behind it: inblob is stored, outblob is missing -> raises.
    with pytest.raises(TeeError):
        tsm_report(bytes(64), expect_provider="tdx_guest", root=str(root), remover=lambda p: None)
    with pytest.raises(TeeError):
        tsm_report(bytes(10), expect_provider="tdx_guest", root=str(root))
    with pytest.raises(TeeError):
        tsm_report(bytes(64), expect_provider="tdx_guest", root=str(tmp_path / "absent"))


def test_hardware_for_tier_confidential_fails_closed_when_unconfigured():
    class Cfg:
        tee_cpu = None
        nvidia_cc = False
        nvidia_attest_cmd = []

    with pytest.raises(TeeError):
        hardware_for_tier(Cfg(), "confidential")
    Cfg.tee_cpu = "tdx"
    with pytest.raises(TeeError):
        hardware_for_tier(Cfg(), "confidential")  # nvidia_cc off


def test_provider_shape():
    class Cpu:
        flavor = "tdx"

        def collect(self, n):
            return {"bound": n}

    flavor, ev = confidential_provider(Cpu(), NvidiaGpuCollector(["t"], runner=_runner(_gpu_json())))(NONCE)
    assert flavor == "confidential" and set(ev) == {"cpu_flavor", "cpu", "gpu"} and ev["cpu_flavor"] == "tdx"
    assert ev["cpu"]["bound"] == gpu_bound_nonce(NONCE, ev["gpu"])


def test_snp_kds_turin_is_refused_and_milan_url_uses_documented_layout():
    report = bytearray(0x4A0)
    report[0x180:0x188] = bytes([3, 0, 0, 0, 0, 0, 7, 9])  # bl=3 tee=0 snp=7 ucode=9 (Milan/Genoa layout)
    report[0x1A0:0x1E0] = bytes(range(64))
    with pytest.raises(TeeError):
        SevSnpCollector(product="Turin")._certs(bytes(report))
    urls = []

    def fetch(url):
        urls.append(url)
        raise TeeError("stop")

    with pytest.raises(TeeError):
        SevSnpCollector(product="Milan", fetcher=fetch)._certs(bytes(report))
    assert urls == [f"https://kdsintf.amd.com/vcek/v1/Milan/{bytes(range(64)).hex()}?blSPL=03&teeSPL=00&snpSPL=07&ucodeSPL=09"]
