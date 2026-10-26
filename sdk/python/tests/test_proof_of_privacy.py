"""Proof-of-Privacy (ADR-0005): customer-side encryption and offline receipt verification."""

import copy
import json

import pytest

from computecoin.ccverify import _node, hash_leaf, main, verify_receipt
from computecoin.crypto import (
    decrypt_job_output,
    generate_customer_keypair,
    prepare_private_job,
)
from computecoin.privacy_crypto import (
    INPUT_AAD,
    PrivacyCryptoError,
    canonical_json,
    decrypt_data,
    generate_ed25519_keypair,
    generate_x25519_keypair,
    open_sealed,
    output_aad,
    reconstruct_secret,
    seal,
    share_aad,
    sign,
)

VERDICT_FIELDS = (
    "verdict_id", "machine_id", "image_hash", "tier_reached", "ephemeral_public_key",
    "receipt_signing_public_key", "nonce", "issued_at", "expires_at", "verifier_public_key",
)


def _guardians():
    keys = {name: generate_x25519_keypair() for name in ("computecoin", "partner", "customer")}
    return {n: k[1] for n, k in keys.items()}, {n: k[0] for n, k in keys.items()}


def test_customer_keys_are_x25519_and_distinct():
    a, b = generate_customer_keypair(), generate_customer_keypair()
    assert len(a[0]) == len(a[1]) == 64 and a != b


def test_prepare_private_job_never_contains_plaintext_and_guardians_can_rebuild_the_key():
    pubs, privs = _guardians()
    _, cust_pub = generate_customer_keypair()
    body = prepare_private_job({"prompt": "secret merger"}, guardians=pubs, threshold=2, result_public_key=cust_pub)
    assert body["input"] == {}
    assert "secret merger" not in json.dumps(body)
    assert all(set(v) == {"x", "envelope"} for v in body["dk_shares"].values())
    # Any two guardians together rebuild the Data Key; each opens only its own envelope.
    shares = [(body["dk_shares"][n]["x"], open_sealed(privs[n], body["dk_shares"][n]["envelope"], share_aad(n))) for n in ("computecoin", "partner")]
    dk = reconstruct_secret(shares, k=2)
    assert json.loads(decrypt_data(dk, body["encrypted_input"], INPUT_AAD)) == {"prompt": "secret merger"}
    with pytest.raises(PrivacyCryptoError):
        open_sealed(privs["partner"], body["dk_shares"]["computecoin"]["envelope"], share_aad("computecoin"))


def test_output_needs_the_customer_private_key_and_the_right_job():
    priv, pub = generate_customer_keypair()
    out = seal(pub, json.dumps({"answer": 42}).encode(), output_aad("job-1"))
    assert decrypt_job_output(out, priv, job_id="job-1") == {"answer": 42}
    with pytest.raises(PrivacyCryptoError):
        decrypt_job_output(out, generate_customer_keypair()[0], job_id="job-1")
    with pytest.raises(PrivacyCryptoError):
        decrypt_job_output(out, priv, job_id="job-2")


# --- a receipt built the way the server builds it -----------------------------------------

def _mth(leaves):
    if len(leaves) == 1:
        return leaves[0]
    k = 1
    while k << 1 < len(leaves):
        k <<= 1
    return _node(_mth(leaves[:k]), _mth(leaves[k:]))


def _path(m, leaves):
    if len(leaves) <= 1:
        return []
    k = 1
    while k << 1 < len(leaves):
        k <<= 1
    return _path(m, leaves[:k]) + [_mth(leaves[k:])] if m < k else _path(m - k, leaves[k:]) + [_mth(leaves[:k])]


def _receipt():
    vpriv, vpub = generate_ed25519_keypair()
    rpriv, rpub = generate_ed25519_keypair()
    verdict = {
        "verdict_id": "v1", "machine_id": "m1", "image_hash": "ab" * 32, "tier_reached": "sealed",
        "ephemeral_public_key": generate_x25519_keypair()[1], "receipt_signing_public_key": rpub,
        "nonce": "n" * 16, "issued_at": "2026-10-26T10:00:00+00:00", "expires_at": "2026-10-26T10:30:00+00:00",
        "verifier_public_key": vpub,
    }
    verdict["signature"] = sign(vpriv, canonical_json({f: verdict[f] for f in VERDICT_FIELDS}))
    wipe = {"machine_id": "m1", "job_id": "j1", "wiped_items": ["key_material"], "wiped_at": "2026-10-26T10:05:00+00:00"}
    wipe["signature"] = sign(rpriv, canonical_json(wipe))
    entries = [
        ("IMAGE_HASH", {"image_hash": "ab" * 32}),
        ("ATTESTATION_VERDICT", verdict),
        ("KEY_RELEASE", {"guardian": "computecoin", "job_id": "j1", "verdict_id": "v1", "share_index": 1}),
        ("KEY_RELEASE", {"guardian": "partner", "job_id": "j1", "verdict_id": "v1", "share_index": 2}),
        ("WIPE_RECEIPT", wipe),
    ]
    leaves = [hash_leaf(e, p) for e, p in entries]
    root = _mth(leaves)

    def proof(i):
        return {"leaf_index": i, "tree_size": len(leaves), "leaf_hash": leaves[i], "root_hash": root,
                "audit_path": _path(i, leaves), "event_type": entries[i][0], "payload": entries[i][1]}

    receipt = {
        "job_id": "j1", "machine_id": "m1", "isolation_tier": "sealed", "image_hash": "ab" * 32,
        "attestation_verdict": verdict, "wipe_receipt": wipe, "merkle_root": root,
        "inclusion_proofs": {"attestation_verdict": proof(1), "key_release:computecoin": proof(2), "key_release:partner": proof(3), "wipe_receipt": proof(4)},
    }
    return receipt, vpub


def test_genuine_receipt_verifies():
    receipt, vpub = _receipt()
    result = verify_receipt(receipt, vpub, expected_image_hash="ab" * 32)
    assert result["verified"] is True, result


@pytest.mark.parametrize(
    "attack",
    [
        "forged_verdict_other_key",
        "wrong_verifier_key",
        "edited_logged_payload",
        "proof_claims_other_index",
        "edited_wipe_receipt",
        "tier_upgrade",
        "release_for_other_job",
    ],
)
def test_tampered_receipts_fail(attack):
    receipt, vpub = _receipt()
    r = copy.deepcopy(receipt)
    if attack == "forged_verdict_other_key":
        fpriv, fpub = generate_ed25519_keypair()
        v = r["attestation_verdict"]
        v["verifier_public_key"] = fpub
        v["signature"] = sign(fpriv, canonical_json({f: v[f] for f in VERDICT_FIELDS}))
    elif attack == "wrong_verifier_key":
        vpub = generate_ed25519_keypair()[1]
    elif attack == "edited_logged_payload":
        r["inclusion_proofs"]["key_release:partner"]["payload"]["share_index"] = 3
    elif attack == "proof_claims_other_index":
        r["inclusion_proofs"]["wipe_receipt"]["leaf_index"] = 3
    elif attack == "edited_wipe_receipt":
        r["wipe_receipt"]["wiped_items"] = ["gpu_memory", "key_material"]
    elif attack == "tier_upgrade":
        r["isolation_tier"] = "confidential"
    elif attack == "release_for_other_job":
        r["job_id"] = "j2"
        r["wipe_receipt"]["job_id"] = "j2"
    assert verify_receipt(r, vpub)["verified"] is False


def test_cli_requires_verifier_key_and_reports(tmp_path, capsys):
    receipt, vpub = _receipt()
    path = tmp_path / "r.json"
    path.write_text(json.dumps(receipt))
    assert main([str(path), "--verifier-key", vpub]) == 0
    assert "VERIFIED" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main([str(path)])
