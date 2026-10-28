"""Tests for ccverify on-chain anchoring verification (WP-C / C6)."""

from __future__ import annotations

import json
from unittest.mock import patch
import httpx
import pytest

from computecoin.ccverify import check_on_chain_anchor, verify_consistency_proof, verify_receipt
from computecoin.privacy_crypto import generate_ed25519_keypair, sign, canonical_json


def _make_dummy_receipt(tree_size: int = 2, root: str = "aa" * 32):
    sign_priv, sign_pub = generate_ed25519_keypair()
    verdict = {
        "verdict_id": "v-1",
        "job_id": "job-123",
        "machine_id": "m-1",
        "image_hash": "ee" * 32,
        "tier_reached": "sealed",
        "ephemeral_public_key": "11" * 32,
        "receipt_signing_public_key": "22" * 32,
        "nonce": "nonce-1",
        "issued_at": "2026-10-28T00:00:00Z",
        "expires_at": "2026-10-28T01:00:00Z",
        "verifier_public_key": sign_pub,
    }
    verdict_fields = (
        "verdict_id", "job_id", "machine_id", "image_hash", "tier_reached",
        "ephemeral_public_key", "receipt_signing_public_key", "nonce",
        "issued_at", "expires_at", "verifier_public_key",
    )
    sig = sign(sign_priv, canonical_json({f: verdict[f] for f in verdict_fields}))
    verdict["signature"] = sig

    import hashlib
    leaf_hash = hashlib.sha256(b"\x00ATTESTATION_VERDICT:" + canonical_json(verdict)).hexdigest()

    receipt = {
        "job_id": "job-123",
        "isolation_tier": "sealed",
        "machine_id": "m-1",
        "image_hash": "ee" * 32,
        "attestation_verdict": verdict,
        "merkle_root": root,
        "inclusion_proofs": {
            "attestation_verdict": {
                "event_type": "ATTESTATION_VERDICT",
                "payload": verdict,
                "leaf_hash": leaf_hash,
                "leaf_index": 0,
                "tree_size": tree_size,
                "audit_path": ["bb" * 32],
            }
        },
    }
    # Mock inclusion check pass by setting root = _node(leaf_hash, 'bb'*32)
    from computecoin.ccverify import _node
    receipt["merkle_root"] = _node(leaf_hash, "bb" * 32)
    return receipt, sign_pub


def test_ccverify_without_rpc_ignores_anchor():
    """Without --rpc, receipt verification succeeds even without an anchor section."""
    receipt, verifier_key = _make_dummy_receipt()
    res = verify_receipt(receipt, verifier_key)
    assert res["verified"] is True
    assert "anchored_on_chain" not in res["checks"]


def test_ccverify_with_rpc_missing_anchor_fails():
    """With --rpc but no anchor section, report not_yet_anchored and verified=False."""
    receipt, verifier_key = _make_dummy_receipt()
    res = verify_receipt(receipt, verifier_key, rpc_url="http://fake-rpc.test", vault_address="0x" + "1" * 40)
    assert res["verified"] is False
    assert res["checks"]["anchored_on_chain"] is False
    assert res["checks"].get("anchor_status") == "not_yet_anchored"


def test_ccverify_with_rpc_valid_anchor_passes():
    """With valid anchor, consistency proof, and on-chain record, verify_receipt passes."""
    receipt, verifier_key = _make_dummy_receipt(tree_size=2)
    receipt_root = receipt["merkle_root"]
    anchored_root = "99" * 32

    # Consistency proof from size 2 to size 3
    # MTH(2) = receipt_root
    # MTH(3) = _node(receipt_root, leaf2)
    from computecoin.ccverify import _node
    leaf2 = "cc" * 32
    anchored_root = _node(receipt_root, leaf2)
    consistency_proof = [leaf2]

    receipt["anchor"] = {
        "tree_size": 3,
        "merkle_root": anchored_root,
        "tx_hash": "0x" + "f" * 64,
        "block_number": 500,
        "consistency_proof": consistency_proof,
    }

    # Mock RPC eth_call returning non-zero timestamp
    def mock_post(url, json=None, timeout=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x671eed00"})

    with patch("httpx.post", side_effect=mock_post):
        res = verify_receipt(
            receipt,
            verifier_key,
            rpc_url="http://fake-rpc.test",
            vault_address="0x" + "1" * 40,
        )

    assert res["checks"]["anchor_consistency"] is True
    assert res["checks"]["anchored_on_chain"] is True
    assert res["verified"] is True


def test_ccverify_with_rpc_tampered_anchor_fails():
    """Tampered consistency proof or unanchored root fails verification."""
    receipt, verifier_key = _make_dummy_receipt(tree_size=2)
    receipt["anchor"] = {
        "tree_size": 3,
        "merkle_root": "00" * 32,
        "tx_hash": "0x" + "f" * 64,
        "block_number": 500,
        "consistency_proof": ["bad_proof" * 4],
    }

    def mock_post(url, json=None, timeout=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x0"})

    with patch("httpx.post", side_effect=mock_post):
        res = verify_receipt(
            receipt,
            verifier_key,
            rpc_url="http://fake-rpc.test",
            vault_address="0x" + "1" * 40,
        )

    assert res["checks"]["anchor_consistency"] is False
    assert res["checks"]["anchored_on_chain"] is False
    assert res["verified"] is False
