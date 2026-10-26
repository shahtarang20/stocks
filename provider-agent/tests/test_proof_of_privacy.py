"""Proof-of-Privacy (ADR-0005) on the provider agent.

Covers the properties that matter, not just round trips:
- only the in-RAM private key opens a released share (the public key is useless to an attacker);
- a share released for one job cannot be used for another;
- the result can be read only with the customer's private key;
- the wipe receipt is Ed25519-signed and lists only what was actually wiped;
- key material is zeroed even when the workload fails;
- tier probes fail closed.
"""

import json
import os
import secrets

import pytest

from computecoin_agent.attestation import generate_ephemeral_keypair, generate_receipt_signing_keypair
from computecoin_agent.isolation import (
    TIER_CONTAINER,
    TIER_SEALED,
    TIER_SEALED_PRO,
    detect_supported_isolation_tiers,
    probe_bare_metal,
    probe_memory_encryption,
)
from computecoin_agent.privacy import execute_sealed_job, wipe_environment, wipe_receipt_signing_bytes
from computecoin_agent.privacy_crypto import (
    INPUT_AAD,
    PrivacyCryptoError,
    encrypt_data,
    open_sealed,
    output_aad,
    release_aad,
    seal,
    split_secret,
    verify,
    x25519_public_from_private,
)


def _customer_job(prompt: str = "TOP SECRET"):
    dk = secrets.token_bytes(32)
    enc = encrypt_data(dk, json.dumps({"prompt": prompt}).encode(), INPUT_AAD)
    return dk, enc


def _release(dk: bytes, host_pub: str, job_id: str, guardians=("computecoin", "partner")):
    shares = split_secret(dk, n=3, k=2)
    return [
        {"guardian": g, "share_index": x, "wrapped_share": seal(host_pub, s, release_aad(job_id, g))}
        for g, (x, s) in zip(guardians, shares)
    ]


def test_ephemeral_and_receipt_keys_are_real_and_fresh():
    priv1, pub1 = generate_ephemeral_keypair()
    priv2, pub2 = generate_ephemeral_keypair()
    assert len(pub1) == 64 and pub1 != pub2 and priv1 != priv2
    assert x25519_public_from_private(priv1) == pub1
    spriv, spub = generate_receipt_signing_keypair()
    from computecoin_agent.privacy_crypto import sign

    assert verify(spub, b"m", sign(spriv, b"m"))
    assert not verify(spub, b"other", sign(spriv, b"m"))


def test_sealed_job_end_to_end_only_customer_reads_result():
    host_priv, host_pub = generate_ephemeral_keypair()
    rpriv, rpub = generate_receipt_signing_keypair()
    cust_priv, cust_pub = generate_ephemeral_keypair()
    dk, enc = _customer_job("summarize the merger memo")
    seen = {}

    def runner(data):
        seen.update(data)
        return {"summary": "done"}

    out, wipe = execute_sealed_job(
        job_id="job-1", machine_id="m-1",
        ephemeral_private_key=host_priv, receipt_signing_private_key=rpriv, result_public_key=cust_pub,
        encrypted_input=enc, wrapped_shares=_release(dk, host_pub, "job-1"), workload_runner_fn=runner,
    )
    assert seen == {"prompt": "summarize the merger memo"}
    assert json.loads(open_sealed(cust_priv, out, output_aad("job-1"))) == {"summary": "done"}
    with pytest.raises(PrivacyCryptoError):  # the host (or anyone else) cannot read the result
        open_sealed(host_priv, out, output_aad("job-1"))
    assert verify(rpub, wipe_receipt_signing_bytes(wipe), wipe["signature"])
    assert wipe["wiped_items"] == ["key_material"]


def test_public_key_alone_cannot_open_a_released_share():
    _, host_pub = generate_ephemeral_keypair()
    dk, _ = _customer_job()
    wrapped = _release(dk, host_pub, "job-1")[0]
    attacker_priv, _ = generate_ephemeral_keypair()
    with pytest.raises(PrivacyCryptoError):
        open_sealed(attacker_priv, wrapped["wrapped_share"], release_aad("job-1", wrapped["guardian"]))


def test_share_released_for_one_job_is_useless_for_another():
    host_priv, host_pub = generate_ephemeral_keypair()
    rpriv, _ = generate_receipt_signing_keypair()
    _, cust_pub = generate_ephemeral_keypair()
    dk, enc = _customer_job()
    with pytest.raises(PrivacyCryptoError):
        execute_sealed_job(
            job_id="job-OTHER", machine_id="m", ephemeral_private_key=host_priv, receipt_signing_private_key=rpriv,
            result_public_key=cust_pub, encrypted_input=enc, wrapped_shares=_release(dk, host_pub, "job-1"),
            workload_runner_fn=lambda d: d,
        )


def test_key_material_is_wiped_even_when_the_workload_fails():
    host_priv, host_pub = generate_ephemeral_keypair()
    rpriv, _ = generate_receipt_signing_keypair()
    _, cust_pub = generate_ephemeral_keypair()
    dk, enc = _customer_job()
    gpu_resets = []

    def boom(_):
        raise RuntimeError("workload crashed")

    with pytest.raises(RuntimeError, match="workload crashed"):
        execute_sealed_job(
            job_id="j", machine_id="m", ephemeral_private_key=host_priv, receipt_signing_private_key=rpriv,
            result_public_key=cust_pub, encrypted_input=enc, wrapped_shares=_release(dk, host_pub, "j"),
            workload_runner_fn=boom, gpu_reset_fn=lambda: gpu_resets.append(1) or True,
        )
    assert gpu_resets == [1]


def test_tampered_input_is_rejected():
    host_priv, host_pub = generate_ephemeral_keypair()
    rpriv, _ = generate_receipt_signing_keypair()
    _, cust_pub = generate_ephemeral_keypair()
    dk, enc = _customer_job()
    enc = dict(enc, ct=("00" if enc["ct"][:2] != "00" else "11") + enc["ct"][2:])
    with pytest.raises(PrivacyCryptoError):
        execute_sealed_job(
            job_id="j", machine_id="m", ephemeral_private_key=host_priv, receipt_signing_private_key=rpriv,
            result_public_key=cust_pub, encrypted_input=enc, wrapped_shares=_release(dk, host_pub, "j"),
            workload_runner_fn=lambda d: d,
        )


def test_wipe_receipt_lists_only_what_was_done(tmp_path):
    rpriv, rpub = generate_receipt_signing_keypair()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "f").write_text("secret")
    buf = bytearray(b"secretkey")
    r = wipe_environment(machine_id="m", job_id="j", receipt_signing_private_key=rpriv, memory_buffers=[buf], scratch_dir=str(scratch))
    assert buf == bytearray(len(buf))
    assert not scratch.exists()
    assert r["wiped_items"] == ["key_material", "scratch_dir"]  # no claim about GPU memory
    assert verify(rpub, wipe_receipt_signing_bytes(r), r["signature"])
    forged = dict(r, wiped_items=["gpu_memory", "key_material", "scratch_dir"])
    assert not verify(rpub, wipe_receipt_signing_bytes(forged), r["signature"])


def test_probes_fail_closed():
    assert probe_memory_encryption(cpuinfo="flags : fpu sme", kernel_log="") is False
    assert probe_memory_encryption(cpuinfo="flags : fpu sme", kernel_log="AMD Memory Encryption Features active: SME") is True
    assert probe_memory_encryption(cpuinfo="flags : fpu tme", kernel_log="x86/tme: enabled by BIOS") is True
    assert probe_bare_metal(cpuinfo="flags : fpu hypervisor", dmi_values=["Z790", "MSI"]) is False
    assert probe_bare_metal(cpuinfo="flags : fpu", dmi_values=["Virtual Machine", "Microsoft Corporation"]) is False
    assert probe_bare_metal(cpuinfo="flags : fpu sse", dmi_values=["Z790 AORUS", "Gigabyte"]) is True


def test_tier_detection_with_injected_probes():
    off = lambda: False  # noqa: E731
    on = lambda: True  # noqa: E731
    base = dict(probe_gvisor_fn=off, probe_microvm_fn=off, probe_cc_fn=off)
    assert detect_supported_isolation_tiers(**base) == [TIER_CONTAINER]
    assert TIER_SEALED in detect_supported_isolation_tiers(**base, probe_tpm_fn=on, probe_mem_enc_fn=off)
    tiers = detect_supported_isolation_tiers(**base, probe_tpm_fn=on, probe_mem_enc_fn=on)
    assert TIER_SEALED in tiers and TIER_SEALED_PRO in tiers
