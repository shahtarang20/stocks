"""ccverify witness checks (WP-E / E6).

The receipt's own tree is older than any checkpoint a witness can still cosign (a witness never
cosigns a size below one it has seen), so a receipt carries a LATER witnessed checkpoint plus a
consistency proof from its tree to it. These tests build a real log: the receipt is at size 2 and
the witnessed checkpoint at size 5 unless stated otherwise.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from computecoin.ccverify import _node, main, verify_receipt, witness_statement
from computecoin.privacy_crypto import canonical_json, generate_ed25519_keypair, sign

LOG_KEY = "55" * 32
VERDICT_FIELDS = (
    "verdict_id", "job_id", "machine_id", "image_hash", "tier_reached",
    "ephemeral_public_key", "receipt_signing_public_key", "nonce",
    "issued_at", "expires_at", "verifier_public_key",
)


def _leaf(event: str, payload: dict) -> str:
    return hashlib.sha256(b"\x00" + event.encode() + b":" + canonical_json(payload)).hexdigest()


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


def _subproof(m, leaves, b):
    n = len(leaves)
    if m == n:
        return [] if b else [_mth(leaves)]
    k = 1
    while k << 1 < n:
        k <<= 1
    if m <= k:
        return _subproof(m, leaves[:k], b) + [_mth(leaves[k:])]
    return _subproof(m - k, leaves[k:], False) + [_mth(leaves[:k])]


class Log:
    """A receipt at ``receipt_size`` in a log that later grew to ``total`` leaves."""

    def __init__(self, receipt_size=2, total=5):
        vpriv, self.verifier_pub = generate_ed25519_keypair()
        verdict = {
            "verdict_id": "v-1", "job_id": "job-123", "machine_id": "m-1", "image_hash": "ee" * 32,
            "tier_reached": "sealed", "ephemeral_public_key": "11" * 32, "receipt_signing_public_key": "22" * 32,
            "nonce": "nonce-1", "issued_at": "2026-10-28T00:00:00Z", "expires_at": "2026-10-28T00:10:00Z",
            "verifier_public_key": self.verifier_pub,
        }
        verdict["signature"] = sign(vpriv, canonical_json({f: verdict[f] for f in VERDICT_FIELDS}))
        self.leaves = [_leaf("ATTESTATION_VERDICT", verdict)] + [_leaf("IMAGE_HASH", {"i": i}) for i in range(1, total)]
        self.receipt_size = receipt_size
        root = _mth(self.leaves[:receipt_size])
        self.receipt = {
            "job_id": "job-123", "isolation_tier": "sealed", "machine_id": "m-1", "image_hash": "ee" * 32,
            "attestation_verdict": verdict, "merkle_root": root,
            "inclusion_proofs": {"attestation_verdict": {
                "event_type": "ATTESTATION_VERDICT", "payload": verdict, "leaf_hash": self.leaves[0],
                "leaf_index": 0, "tree_size": receipt_size, "audit_path": _path(0, self.leaves[:receipt_size]),
            }},
        }

    def witness(self, signers, size=None, leaves=None, log_key=LOG_KEY):
        """Attach a witness section for a checkpoint at ``size`` cosigned by ``signers`` [(name, priv)]."""
        leaves = leaves or self.leaves
        size = size or len(leaves)
        root = _mth(leaves[:size])
        self.receipt["witness"] = {
            "checkpoint": {"v": "ccpp-log-v1", "tree_size": size, "root_hash": root, "timestamp": "t",
                           "log_public_key": log_key, "signature": "00" * 64},
            "consistency_proof": _subproof(self.receipt_size, leaves[:size], True) if self.receipt_size < size else [],
            "cosignatures": [{"witness": n, "signature": sign(k, witness_statement(size, root, log_key, n))} for n, k in signers],
        }
        return self.receipt


def _witnesses(n):
    return [(f"witness-{i}", *generate_ed25519_keypair()) for i in range(1, n + 1)]


def test_threshold_zero_ignores_witnesses():
    log = Log()
    res = verify_receipt(log.receipt, log.verifier_pub, witness_threshold=0)
    assert res["verified"] is True and "witness_cosignatures" not in res["checks"]


@pytest.mark.parametrize("receipt_size,total", [(2, 5), (3, 3), (1, 8), (4, 7)])
def test_a_later_witnessed_checkpoint_verifies_an_older_receipt(receipt_size, total):
    log = Log(receipt_size, total)
    ws = _witnesses(2)
    receipt = log.witness([(n, priv) for n, priv, _ in ws])
    res = verify_receipt(receipt, log.verifier_pub, witness_keys={n: pub for n, _, pub in ws}, witness_threshold=2, log_public_key=LOG_KEY)
    assert res["checks"]["witness_cosignatures"] is True and res["verified"] is True, res


def test_below_threshold_fails():
    log = Log()
    ws = _witnesses(2)
    receipt = log.witness([(ws[0][0], ws[0][1])])
    res = verify_receipt(receipt, log.verifier_pub, witness_keys={n: pub for n, _, pub in ws}, witness_threshold=2)
    assert res["checks"]["witness_cosignatures"] is False and res["verified"] is False


def test_no_witness_section_fails_when_witnesses_are_required():
    log = Log()
    res = verify_receipt(log.receipt, log.verifier_pub, witness_keys={"w": "11" * 32}, witness_threshold=1)
    assert res["checks"]["witness_cosignatures"] is False and res["verified"] is False


def test_forged_cosignature_fails():
    log = Log()
    (name, _, pub), = _witnesses(1)
    forger, _ = generate_ed25519_keypair()
    receipt = log.witness([(name, forger)])
    assert verify_receipt(receipt, log.verifier_pub, witness_keys={name: pub}, witness_threshold=1)["verified"] is False


def test_one_key_listed_under_two_names_counts_once():
    log = Log()
    priv, pub = generate_ed25519_keypair()
    receipt = log.witness([("a", priv), ("b", priv)])
    res = verify_receipt(receipt, log.verifier_pub, witness_keys={"a": pub, "b": pub}, witness_threshold=2)
    assert res["checks"]["witness_cosignatures"] is False


def test_a_forked_log_is_not_consistent_with_the_receipt():
    """Witnesses cosigned a log that does not contain the receipt's tree: the receipt fails."""
    log = Log()
    ws = _witnesses(1)
    forked = [_leaf("IMAGE_HASH", {"forged": i}) for i in range(5)]
    receipt = log.witness([(ws[0][0], ws[0][1])], leaves=forked)
    receipt["witness"]["consistency_proof"] = _subproof(2, log.leaves[:5], True)  # an honest-looking proof
    res = verify_receipt(receipt, log.verifier_pub, witness_keys={ws[0][0]: ws[0][2]}, witness_threshold=1)
    assert res["checks"]["witness_cosignatures"] is False


def test_tampered_consistency_proof_fails():
    log = Log()
    ws = _witnesses(1)
    receipt = log.witness([(ws[0][0], ws[0][1])])
    receipt["witness"]["consistency_proof"][0] = "00" * 32
    assert verify_receipt(receipt, log.verifier_pub, witness_keys={ws[0][0]: ws[0][2]}, witness_threshold=1)["verified"] is False


def test_pinned_log_key_must_match():
    log = Log()
    ws = _witnesses(1)
    receipt = log.witness([(ws[0][0], ws[0][1])], log_key="66" * 32)
    keys = {ws[0][0]: ws[0][2]}
    assert verify_receipt(receipt, log.verifier_pub, witness_keys=keys, witness_threshold=1)["verified"] is True
    assert verify_receipt(receipt, log.verifier_pub, witness_keys=keys, witness_threshold=1, log_public_key=LOG_KEY)["verified"] is False


@pytest.mark.parametrize("breakage", ["no_checkpoint", "size_not_int", "proof_not_list"])
def test_malformed_witness_sections_fail_without_crashing(breakage):
    log = Log()
    ws = _witnesses(1)
    receipt = copy.deepcopy(log.witness([(ws[0][0], ws[0][1])]))
    if breakage == "no_checkpoint":
        del receipt["witness"]["checkpoint"]
    elif breakage == "size_not_int":
        receipt["witness"]["checkpoint"]["tree_size"] = "5"
    else:
        receipt["witness"]["consistency_proof"] = "ab"
    assert verify_receipt(receipt, log.verifier_pub, witness_keys={ws[0][0]: ws[0][2]}, witness_threshold=1)["verified"] is False


def test_cli_witness_arguments(tmp_path):
    log = Log()
    ws = _witnesses(1)
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(log.witness([(ws[0][0], ws[0][1])])))
    keys_file = tmp_path / "witnesses.json"
    keys_file.write_text(json.dumps({ws[0][0]: ws[0][2]}))
    base = [str(receipt_file), "--verifier-key", log.verifier_pub]
    assert main(base + ["--witness-keys", f"{ws[0][0]}={ws[0][2]}", "--witness-threshold", "1"]) == 0
    assert main(base + ["--witness-keys", str(keys_file), "--witness-threshold", "1", "--log-key", LOG_KEY]) == 0
    assert main(base + ["--witness-keys", str(keys_file), "--witness-threshold", "2"]) == 1
    assert main(base + ["--witness-keys", str(keys_file), "--witness-threshold", "1", "--log-key", "77" * 32]) == 1
