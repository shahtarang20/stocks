"""Test suite validating ccpp-v1 cryptographic test vectors (WP1, Checkpoints 1.2 & 1.4)."""

import json
import pathlib
import pytest

from computecoin_agent.privacy_crypto import (
    PrivacyCryptoError,
    canonical_json,
    compute_output_commitment,
    decrypt_data,
    derive_output_commitment_key,
    ed25519_public_from_private,
    open_sealed,
    reconstruct_secret,
    sign,
    verify,
    x25519_public_from_private,
)


@pytest.fixture
def vectors():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    vector_file = repo_root / "docs/specs/ccpp-v1-vectors.json"
    with open(vector_file, "r") as f:
        return json.load(f)


def test_x25519_vector(vectors):
    v = vectors["x25519"]
    derived_pub = x25519_public_from_private(v["private_key_hex"])
    assert derived_pub == v["expected_public_key_hex"]


def test_ed25519_vector(vectors):
    v = vectors["ed25519"]
    derived_pub = ed25519_public_from_private(v["private_seed_hex"])
    assert derived_pub == v["expected_public_key_hex"]

    msg = bytes.fromhex(v["message_hex"])
    sig = sign(v["private_seed_hex"], msg)
    assert sig == v["expected_signature_hex"]
    assert verify(v["expected_public_key_hex"], msg, sig)


def test_sealing_vector_and_opening(vectors):
    v = vectors["sealing"]
    aad = bytes.fromhex(v["aad_hex"])
    pt = open_sealed(v["recipient_private_hex"], v["envelope"], aad)
    assert pt == bytes.fromhex(v["plaintext_hex"])


def test_data_encryption_vector_and_decryption(vectors):
    v = vectors["data_encryption"]
    data_key = bytes.fromhex(v["data_key_hex"])
    aad = bytes.fromhex(v["aad_hex"])
    pt = decrypt_data(data_key, v["envelope"], aad)
    assert pt == bytes.fromhex(v["plaintext_hex"])


def test_shamir_vectors_and_reconstruction(vectors):
    v = vectors["shamir"]
    expected_secret = bytes.fromhex(v["secret_hex"])
    shares = [(s["x"], bytes.fromhex(s["share_hex"])) for s in v["shares"]]

    pairs = [(shares[0], shares[1]), (shares[1], shares[2]), (shares[0], shares[2])]
    for p in pairs:
        recovered = reconstruct_secret(list(p), k=v["threshold"])
        assert recovered == expected_secret


def test_output_commitment_vector(vectors):
    v = vectors["output_commitment"]
    data_key = bytes.fromhex(v["data_key_hex"])
    commit_key = derive_output_commitment_key(data_key)
    assert commit_key.hex() == v["expected_commit_key_hex"]

    commitment = compute_output_commitment(data_key, v["output_json"])
    assert commitment == v["expected_commitment_hex"]


def test_mutation_one_byte_causes_failure(vectors):
    """Checkpoint 1.4: A deliberate 1-byte change in any vector makes verification/decryption fail."""
    seal_v = vectors["sealing"]
    mutated_envelope = dict(seal_v["envelope"])
    ct_bytes = bytearray.fromhex(mutated_envelope["ct"])
    ct_bytes[0] ^= 0x01
    mutated_envelope["ct"] = bytes(ct_bytes).hex()

    with pytest.raises(PrivacyCryptoError):
        open_sealed(seal_v["recipient_private_hex"], mutated_envelope, bytes.fromhex(seal_v["aad_hex"]))

    data_v = vectors["data_encryption"]
    mutated_data_env = dict(data_v["envelope"])
    ct_data_bytes = bytearray.fromhex(mutated_data_env["ct"])
    ct_data_bytes[0] ^= 0x01
    mutated_data_env["ct"] = bytes(ct_data_bytes).hex()

    with pytest.raises(PrivacyCryptoError):
        decrypt_data(bytes.fromhex(data_v["data_key_hex"]), mutated_data_env, bytes.fromhex(data_v["aad_hex"]))

    ed_v = vectors["ed25519"]
    sig_bytes = bytearray.fromhex(ed_v["expected_signature_hex"])
    sig_bytes[0] ^= 0x01
    assert not verify(ed_v["expected_public_key_hex"], bytes.fromhex(ed_v["message_hex"]), bytes(sig_bytes).hex())

    shamir_v = vectors["shamir"]
    shares = [(s["x"], bytearray.fromhex(s["share_hex"])) for s in shamir_v["shares"]]
    shares[0][1][0] ^= 0x01
    corrupt_shares = [(shares[0][0], bytes(shares[0][1])), (shares[1][0], bytes(shares[1][1]))]
    recovered = reconstruct_secret(corrupt_shares, k=shamir_v["threshold"])
    assert recovered != bytes.fromhex(shamir_v["secret_hex"])
