"""Test suite validating ccpp-v1 cryptographic test vectors (WP1, Checkpoints 1.2 & 1.4)."""

import json
import pathlib
import pytest

from app.services.privacy_crypto import (
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
    repo_root = pathlib.Path(__file__).resolve().parents[3]
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

    # Pairwise reconstruction
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
    # 1. Mutate sealing ciphertext
    seal_v = vectors["sealing"]
    mutated_envelope = dict(seal_v["envelope"])
    ct_bytes = bytearray.fromhex(mutated_envelope["ct"])
    ct_bytes[0] ^= 0x01
    mutated_envelope["ct"] = bytes(ct_bytes).hex()

    with pytest.raises(PrivacyCryptoError):
        open_sealed(seal_v["recipient_private_hex"], mutated_envelope, bytes.fromhex(seal_v["aad_hex"]))

    # 2. Mutate data encryption ciphertext
    data_v = vectors["data_encryption"]
    mutated_data_env = dict(data_v["envelope"])
    ct_data_bytes = bytearray.fromhex(mutated_data_env["ct"])
    ct_data_bytes[0] ^= 0x01
    mutated_data_env["ct"] = bytes(ct_data_bytes).hex()

    with pytest.raises(PrivacyCryptoError):
        decrypt_data(bytes.fromhex(data_v["data_key_hex"]), mutated_data_env, bytes.fromhex(data_v["aad_hex"]))

    # 3. Mutate Ed25519 signature
    ed_v = vectors["ed25519"]
    sig_bytes = bytearray.fromhex(ed_v["expected_signature_hex"])
    sig_bytes[0] ^= 0x01
    assert not verify(ed_v["expected_public_key_hex"], bytes.fromhex(ed_v["message_hex"]), bytes(sig_bytes).hex())

    # 4. Mutate Shamir share
    shamir_v = vectors["shamir"]
    shares = [(s["x"], bytearray.fromhex(s["share_hex"])) for s in shamir_v["shares"]]
    shares[0][1][0] ^= 0x01  # corrupt 1 byte of share 1
    corrupt_shares = [(shares[0][0], bytes(shares[0][1])), (shares[1][0], bytes(shares[1][1]))]
    recovered = reconstruct_secret(corrupt_shares, k=shamir_v["threshold"])
    assert recovered != bytes.fromhex(shamir_v["secret_hex"])


def test_official_rfc_vectors():
    """Independent check: our vectors were produced by this code, so also test against the standards."""
    # RFC 7748 section 6.1 (X25519, Alice and Bob).
    alice = "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    bob_pub = "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    assert x25519_public_from_private(alice) == "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    shared = X25519PrivateKey.from_private_bytes(bytes.fromhex(alice)).exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(bob_pub)))
    assert shared.hex() == "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
    # RFC 8032 section 7.1, TEST 1 (Ed25519, empty message).
    seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
    pub = ed25519_public_from_private(seed)
    assert pub == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    assert sign(seed, b"") == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )


def test_sealing_vector_intermediate_values(vectors):
    """The vector's shared secret and seal key are checked too, not only that the envelope opens."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    from app.services.privacy_crypto import _seal_key

    v = vectors["sealing"]
    eph = X25519PrivateKey.from_private_bytes(bytes.fromhex(v["ephemeral_private_hex"]))
    assert eph.public_key().public_bytes_raw().hex() == v["ephemeral_public_hex"]
    shared = eph.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(v["recipient_public_hex"])))
    assert shared.hex() == v["expected_shared_secret_hex"]
    key = _seal_key(shared, bytes.fromhex(v["ephemeral_public_hex"]), bytes.fromhex(v["recipient_public_hex"]))
    assert key.hex() == v["expected_seal_key_hex"]
