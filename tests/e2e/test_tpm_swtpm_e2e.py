"""TPM 2.0 hardware attestation end to end, on a real TPM 2.0 implementation (swtpm + tpm2-tools).

Runs when swtpm and tpm2-tools are available: on PATH, at $COMPUTECOIN_TPM_TOOLS_ROOT, or unpacked
at <repo>/.tools/tpm/root (Ubuntu packages swtpm, swtpm-tools, libtpms0, tpm2-tools,
libtss2-fapi1t64 extracted with dpkg-deb -x; nothing installed system-wide). Skipped otherwise.

Flow: the agent's Tpm2 creates the EK and AK inside the TPM; a test "TPM vendor" CA issues the EK
certificate and is pinned in PRIVACY_HARDWARE_ROOT_CERTS; the AK is enrolled over HTTP with the
API's MakeCredential and the TPM's ActivateCredential; a hardware verdict is issued from a real
quote over the TPM's real PCRs; and it is refused for a replayed quote, an un-enrolled AK, and a
PCR that changed (a different boot).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from httpx import AsyncClient

from app.core.config import settings
from app.models.privacy import TrustedImage
from computecoin_agent.attestation import generate_attestation_report, generate_ephemeral_keypair, generate_receipt_signing_keypair
from computecoin_agent.tpm import AK_HANDLE, Tpm2

from .test_proof_of_privacy_e2e import (
    TEST_LOCKS,
    _agent_headers,
    _configure_privacy_environment,
    _seed_infrastructure,
    _sm,
)

REPO = Path(__file__).resolve().parents[2]
TOOLS_ROOT = os.environ.get("COMPUTECOIN_TPM_TOOLS_ROOT") or (str(REPO / ".tools/tpm/root") if (REPO / ".tools/tpm/root/usr/bin/swtpm").exists() else None)
HAVE_TOOLS = bool(TOOLS_ROOT) or bool(shutil.which("swtpm") and shutil.which("tpm2_quote"))
pytestmark = pytest.mark.skipif(not HAVE_TOOLS, reason="swtpm / tpm2-tools not available")
WORK = REPO / ".tools/tpm"  # the project's own (git-ignored) folder; nothing outside the repo is used


def _env():
    env = dict(os.environ)
    if TOOLS_ROOT:
        env["PATH"] = f"{TOOLS_ROOT}/usr/bin{os.pathsep}{env.get('PATH', '')}"
        env["LD_LIBRARY_PATH"] = f"{TOOLS_ROOT}/usr/lib/x86_64-linux-gnu{os.pathsep}{TOOLS_ROOT}/usr/lib/x86_64-linux-gnu/swtpm"
    return env


def _free_port_pair() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with socket.socket() as s2:
            try:
                s2.bind(("127.0.0.1", port + 1))
                return port
            except OSError:
                continue


@pytest.fixture
def swtpm(monkeypatch):
    WORK.mkdir(parents=True, exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="swtpm-e2e-", dir=WORK))
    monkeypatch.setattr(tempfile, "tempdir", str(state))  # the agent's scratch dirs stay in the project too
    port = _free_port_pair()
    proc = subprocess.Popen(
        ["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={state}", "--server", f"type=tcp,port={port}",
         "--ctrl", f"type=tcp,port={port + 1}", "--flags", "not-need-init,startup-clear"],
        env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        yield Tpm2(f"swtpm:host=127.0.0.1,port={port}", tools_root=TOOLS_ROOT, pcrs=[0, 1, 2, 3, 4, 5, 6, 7])
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
        shutil.rmtree(state, ignore_errors=True)


def _vendor_ca_and_ek_cert(tpm: Tpm2) -> tuple[str, str]:
    """A test 'TPM vendor' root, and an EK certificate it issues for this TPM's EK public key."""
    now = dt.datetime.now(dt.timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TPM Vendor Root")])
    root = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(root_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(root_key, hashes.SHA256()))
    ek_pub = serialization.load_pem_public_key(tpm.ek_public_pem())
    ek = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EK")])).issuer_name(name)
          .public_key(ek_pub).serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
          .not_valid_after(now + dt.timedelta(days=30)).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
          .sign(root_key, hashes.SHA256()))
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()  # noqa: E731
    return pem(root), pem(ek)


async def _post(client, machine, key, path, body):
    raw = json.dumps(body, sort_keys=True).encode()
    return await client.post(path, content=raw, headers=_agent_headers(machine.id, key, raw))


async def _attest(client, machine, key, tpm: Tpm2, *, evidence=None):
    raw = b"{}"
    nonce = (await client.post("/v1/agents/attestation/challenge", content=raw, headers=_agent_headers(machine.id, key, raw))).json()["nonce"]
    report = generate_attestation_report(machine_id=str(machine.id), signing_key=key, nonce=nonce, isolation_tier="sealed", custom_measurements={})
    body = {
        "nonce": nonce, "isolation_tier": "sealed", "timestamp": report.timestamp, "pcr_measurements": report.pcr_measurements,
        "platform_quote": report.platform_quote, "signature": report.signature,
        "ephemeral_public_key": generate_ephemeral_keypair()[1], "receipt_signing_public_key": generate_receipt_signing_keypair()[1],
        "security_state": TEST_LOCKS, "hardware_flavor": "tpm2", "hardware_evidence": evidence or tpm.quote(nonce),
    }
    return await _post(client, machine, key, "/v1/agents/attestation/verify", body)


@pytest.mark.asyncio
async def test_tpm_enrolment_and_hardware_verdict_on_a_real_tpm(client: AsyncClient, db_engine, monkeypatch, swtpm):
    tpm = swtpm
    tpm.ensure_keys()
    tpm.ensure_keys()  # idempotent: the persistent EK and AK are reused
    root_pem, ek_pem = _vendor_ca_and_ek_cert(tpm)
    ek_file = Path(tempfile.gettempdir()) / "ek.pem"
    ek_file.write_text(ek_pem)
    tpm.ek_cert_path = str(ek_file)

    _configure_privacy_environment(monkeypatch)
    monkeypatch.setattr(settings, "PRIVACY_SOFTWARE_ATTESTATION", False)
    monkeypatch.setattr(settings, "PRIVACY_HARDWARE_ATTESTATION", True)
    monkeypatch.setattr(settings, "PRIVACY_HARDWARE_ROOT_CERTS", json.dumps({"tpm2": [root_pem]}))
    _, _, _, _, machine, key = await _seed_infrastructure(db_engine)

    # The trusted image pins this TPM's real boot PCRs.
    pcrs = tpm.quote("pcr-read")["pcrs"]
    async with _sm(db_engine)() as db:
        db.add(TrustedImage(image_hash="7e" * 32, version_tag="swtpm-boot", tier="sealed", status="active",
                            expected_pcrs={f"pcr{i}": pcrs[str(i)] for i in (0, 4, 7)}))
        await db.commit()

    # Before enrolment: no AK is bound, so a genuine quote does not produce a verdict.
    r = await _attest(client, machine, key, tpm)
    assert r.status_code == 409 and "tpm2_ak_not_enrolled" in r.text

    # Enrolment: API MakeCredential -> the TPM's ActivateCredential -> AK bound.
    ek_cert, intermediates = tpm.ek_certificate()
    start = await _post(client, machine, key, "/v1/agents/tpm/enrolments",
                        {"ek_cert": ek_cert, "ek_intermediates": intermediates, "ak_public": tpm.ak_public().hex()})
    assert start.status_code == 200, start.text
    challenge = start.json()
    secret = tpm.activate(bytes.fromhex(challenge["credential_blob"]), bytes.fromhex(challenge["encrypted_secret"]))
    done = await _post(client, machine, key, f"/v1/agents/tpm/enrolments/{challenge['enrolment_id']}/activate", {"secret": secret.hex()})
    assert done.status_code == 200 and done.json()["status"] == "active", done.text

    # A real quote over the real PCRs now yields a hardware verdict for the pinned image.
    r = await _attest(client, machine, key, tpm)
    assert r.status_code == 200, r.text
    verdict = r.json()["verdict"]
    assert verdict["image_hash"] == "7e" * 32 and verdict["tier_reached"] == "sealed"

    # A quote made for an earlier challenge cannot be replayed for a new one.
    old = tpm.quote("an-earlier-challenge-0000")
    r = await _attest(client, machine, key, tpm, evidence=old)
    assert r.status_code == 409 and "tpm2_wrong_nonce" in r.text

    # A different boot (PCR 7 extended) matches no trusted image.
    subprocess.run(["tpm2_pcrextend", "7:sha256=" + "ab" * 32], env=dict(_env(), TPM2TOOLS_TCTI=tpm.tcti), check=True,
                   capture_output=True)
    r = await _attest(client, machine, key, tpm)
    assert r.status_code == 409 and "unknown_os_image" in r.text

    # A new AK that was never enrolled is refused even on the same TPM.
    tpm._run("tpm2_evictcontrol", "-C", "o", "-c", AK_HANDLE)
    tpm.ensure_keys()
    r = await _attest(client, machine, key, tpm)
    assert r.status_code == 409 and "tpm2_unbound_ak" in r.text
