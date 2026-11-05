"""U18 (private fan-out receipt, sealed stream relay). Written without being run in the authoring session."""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.services import fanout_privacy as fp
from app.services import stream_relay
from app.services.privacy_crypto import (
    generate_ed25519_keypair,
    generate_x25519_keypair,
    open_sealed,
    seal,
    stream_aad,
)


@pytest.fixture()
def verifier(monkeypatch):
    priv, pub = generate_ed25519_keypair()
    monkeypatch.setattr(settings, "PRIVACY_VERIFIER_SIGNING_KEY", priv)
    return pub


def _parts(n=3):
    return [(i, str(uuid.uuid4()), f"{i:064x}") for i in range(n)]


def test_job_receipt_verifies_and_commits_to_order(verifier):
    r = fp.build_job_receipt(parent_job_id=str(uuid.uuid4()), isolation_tier="sealed", parts=list(reversed(_parts())))
    assert [e["index"] for e in r["part_receipts"]] == [0, 1, 2]
    assert fp.verify_job_receipt(r, verifier_public_hex=verifier)["valid"] is True


def test_job_receipt_tamper_cases_fail(verifier):
    r = fp.build_job_receipt(parent_job_id=str(uuid.uuid4()), isolation_tier="sealed", parts=_parts())
    swapped = {**r, "part_receipts": [r["part_receipts"][1], r["part_receipts"][0], r["part_receipts"][2]]}
    assert not fp.verify_job_receipt(swapped)["valid"]
    bad_hash = {**r, "part_receipts": [{**r["part_receipts"][0], "receipt_hash": "f" * 64}, *r["part_receipts"][1:]]}
    assert not fp.verify_job_receipt(bad_hash)["valid"]
    assert not fp.verify_job_receipt({**r, "isolation_tier": "confidential"})["valid"]
    other_pub = generate_ed25519_keypair()[1]
    assert not fp.verify_job_receipt(r, verifier_public_hex=other_pub)["valid"]
    dropped = {**r, "part_receipts": r["part_receipts"][:2], "part_count": 2}
    assert not fp.verify_job_receipt(dropped)["valid"]


def test_job_receipt_needs_complete_indices(verifier):
    with pytest.raises(fp.PrivateFanoutError):
        fp.build_job_receipt(parent_job_id="p", isolation_tier="sealed", parts=[(0, "a", "0" * 64), (2, "b", "1" * 64)])


def test_part_receipt_hash_changes_with_any_field():
    base = {"job_id": "j", "isolation_tier": "sealed", "image_hash": "a" * 64, "attestation_verdict": {"x": "1"},
            "wipe_receipt": None, "merkle_root": "b" * 64, "inclusion_proofs": {}}
    assert fp.part_receipt_hash(base) != fp.part_receipt_hash({**base, "image_hash": "c" * 64})


def test_validate_private_parts_refuses_wrong_count_and_reused_keys():
    with pytest.raises(fp.PrivateFanoutError) as e:
        fp.validate_private_parts([], 2)
    assert e.value.code == "private_parts_count_mismatch"
    with pytest.raises(fp.PrivateFanoutError):
        fp.validate_private_parts([{"encrypted_input": {}, "dk_shares": {}, "result_public_key": "00" * 32}], 1)


def test_flag_off_by_default():
    assert settings.PRIVACY_FANOUT_ENABLED is False
    assert settings.PRIVACY_STREAMING_ENABLED is False
    assert settings.WINDOWS_VM_ENABLED is False
    assert fp.private_fanout_enabled() is False


def test_sealed_stream_relay_roundtrip_and_refusals(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_STREAM_PAD_BYTES", 0)  # raw (unpadded) chunks; padding is covered below
    job = str(uuid.uuid4())
    priv, pub = generate_x25519_keypair()
    stream_relay.open_stream(job, sealed=True)
    try:
        env = seal(pub, b"hello", stream_aad(job, 0, False))
        assert stream_relay.accept_sealed_chunk(job, 0, env, False) == (True, 1)
        assert stream_relay.accept_sealed_chunk(job, 0, env, False) == (True, 1)  # exact retry
        with pytest.raises(stream_relay.StreamSequenceError):
            stream_relay.accept_sealed_chunk(job, 5, env, False)  # gap
    finally:
        stream_relay.close_stream(job)
    assert open_sealed(priv, env, stream_aad(job, 0, False)) == b"hello"
    with pytest.raises(Exception):
        open_sealed(priv, env, stream_aad(job, 0, True))  # final flag is authenticated
    with pytest.raises(Exception):
        open_sealed(priv, env, stream_aad(job, 1, False))  # position is authenticated


def test_plaintext_stream_refuses_sealed_chunk_and_junk():
    job = str(uuid.uuid4())
    stream_relay.open_stream(job)
    try:
        _, pub = generate_x25519_keypair()
        with pytest.raises(stream_relay.StreamSequenceError):
            stream_relay.accept_sealed_chunk(job, 0, seal(pub, b"x", b"a"), False)
    finally:
        stream_relay.close_stream(job)
    job2 = str(uuid.uuid4())
    stream_relay.open_stream(job2, sealed=True)
    try:
        with pytest.raises(stream_relay.StreamSequenceError):
            stream_relay.accept_sealed_chunk(job2, 0, {"ct": "not an envelope"}, False)
    finally:
        stream_relay.close_stream(job2)


def test_padded_sealed_stream_relay_requires_block_aligned_ciphertext(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_STREAM_PAD_BYTES", 256)
    job = str(uuid.uuid4())
    _priv, pub = generate_x25519_keypair()
    framed = (5).to_bytes(4, "big") + b"hello"
    padded = framed + b"\x00" * (-len(framed) % 256)
    stream_relay.open_stream(job, sealed=True)
    try:
        assert stream_relay.accept_sealed_chunk(job, 0, seal(pub, padded, stream_aad(job, 0, False)), False) == (True, 1)
        with pytest.raises(stream_relay.StreamSequenceError):  # a raw 5-byte chunk would leak its length
            stream_relay.accept_sealed_chunk(job, 1, seal(pub, b"hello", stream_aad(job, 1, False)), False)
    finally:
        stream_relay.close_stream(job)
