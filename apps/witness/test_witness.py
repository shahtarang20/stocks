"""Tests for the independent Log Witness service (ADR-0005 WP-E)."""

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from apps.witness import config as config_module, main as main_module
from apps.witness.crypto import (
    canonical_json,
    sign_ed25519,
    verify_signature,
    hash_internal_node,
)


def _keypair() -> tuple[str, str]:
    priv = ed25519.Ed25519PrivateKey.generate()
    seed = priv.private_bytes_raw().hex()
    pub = priv.public_key().public_bytes_raw().hex()
    return seed, pub


def _make_checkpoint(log_seed: str, log_pub: str, tree_size: int, root_hash: str) -> dict:
    cp = {
        "v": "ccpp-log-v1",
        "tree_size": tree_size,
        "root_hash": root_hash,
        "timestamp": "2026-10-28T10:00:00+00:00",
        "log_public_key": log_pub,
    }
    cp["signature"] = sign_ed25519(log_seed, canonical_json(cp))
    return cp


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    log_seed, log_pub = _keypair()
    wit_seed, wit_pub = _keypair()
    state_file = tmp_path / "witness_state.json"

    monkeypatch.setenv("WITNESS_NAME", "witness-charlie")
    monkeypatch.setenv("WITNESS_PRIVATE_KEY", wit_seed)
    monkeypatch.setenv("LOG_PUBLIC_KEY", log_pub)
    monkeypatch.setenv("WITNESS_STATE_PATH", str(state_file))

    # Reload config instance in main module
    main_module.config = config_module.WitnessConfig()
    client = TestClient(main_module.app)
    return {
        "client": client,
        "log_seed": log_seed,
        "log_pub": log_pub,
        "wit_seed": wit_seed,
        "wit_pub": wit_pub,
        "state_file": state_file,
    }


def test_witness_health_and_state(env):
    client = env["client"]
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["witness"] == "witness-charlie"
    assert resp.json()["public_key"] == env["wit_pub"]

    resp = client.get("/state")
    assert resp.status_code == 200
    assert resp.json() == {"tree_size": 0, "root_hash": None}


def test_first_checkpoint_cosigns_and_updates_state(env):
    client = env["client"]
    root1 = "11" * 32
    cp = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=1, root_hash=root1)

    resp = client.post("/cosign", json={"checkpoint": cp, "consistency_proof": []})
    assert resp.status_code == 200
    data = resp.json()
    assert data["witness"] == "witness-charlie"
    assert data["tree_size"] == 1
    assert data["root_hash"] == root1

    # Verify witness signature
    expected_statement = {
        "v": "ccpp-witness-v1",
        "tree_size": 1,
        "root_hash": root1,
        "log_public_key": env["log_pub"].lower(),
        "witness": "witness-charlie",
    }
    assert verify_signature(
        env["wit_pub"],
        canonical_json(expected_statement),
        data["signature"],
    )

    # State endpoint reflects tree_size 1
    resp = client.get("/state")
    assert resp.json() == {"tree_size": 1, "root_hash": root1}


def test_forward_consistency_and_refusal_of_inconsistent_checkpoint(env):
    client = env["client"]
    leaf1 = "aa" * 32
    leaf2 = "bb" * 32
    root1 = leaf1
    root2 = hash_internal_node(leaf1, leaf2)

    # 1. Cosign size 1
    cp1 = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=1, root_hash=root1)
    res1 = client.post("/cosign", json={"checkpoint": cp1, "consistency_proof": []})
    assert res1.status_code == 200

    # 2. Try to advance to size 2 with BAD consistency proof -> 409 log_inconsistent (E2)
    cp2 = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=2, root_hash=root2)
    bad_res = client.post("/cosign", json={"checkpoint": cp2, "consistency_proof": ["cc" * 32]})
    assert bad_res.status_code == 409
    assert bad_res.json()["detail"] == "log_inconsistent"

    # 3. Advance to size 2 with valid consistency proof (for size 1 to 2, proof is [leaf2])
    good_res = client.post("/cosign", json={"checkpoint": cp2, "consistency_proof": [leaf2]})
    assert good_res.status_code == 200
    assert good_res.json()["tree_size"] == 2
    assert good_res.json()["root_hash"] == root2


def test_refuse_rolled_back_checkpoint(env):
    """E2: Witness refuses a checkpoint smaller than its current size."""
    client = env["client"]
    root = "11" * 32
    cp = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=5, root_hash=root)
    res = client.post("/cosign", json={"checkpoint": cp, "consistency_proof": []})
    assert res.status_code == 200

    # Propose size 4
    smaller_cp = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=4, root_hash="22" * 32)
    res2 = client.post("/cosign", json={"checkpoint": smaller_cp, "consistency_proof": []})
    assert res2.status_code == 409
    assert res2.json()["detail"] == "log_rolled_back"


def test_split_view_detection(env):
    """E3: Witness at (size N, root A) refuses (size N, root B)."""
    client = env["client"]
    root_a = "aa" * 32
    root_b = "bb" * 32

    cp_a = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=3, root_hash=root_a)
    res_a = client.post("/cosign", json={"checkpoint": cp_a, "consistency_proof": []})
    assert res_a.status_code == 200

    # Now attacker presents another checkpoint at tree_size 3 with root_b
    cp_b = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=3, root_hash=root_b)
    res_b = client.post("/cosign", json={"checkpoint": cp_b, "consistency_proof": []})
    assert res_b.status_code == 409
    assert res_b.json()["detail"] == "split_view_detected"


def test_witness_state_survives_restart(env):
    """E7: State file on disk survives and prevents rollback on fresh process load."""
    client = env["client"]
    root = "55" * 32
    cp = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=10, root_hash=root)
    res = client.post("/cosign", json={"checkpoint": cp, "consistency_proof": []})
    assert res.status_code == 200

    # Check file exists and has size 10
    saved = json.loads(env["state_file"].read_text())
    assert saved["tree_size"] == 10
    assert saved["root_hash"] == root

    # Simulate restart: new TestClient, verify state loaded from disk
    new_client = TestClient(main_module.app)
    state_res = new_client.get("/state")
    assert state_res.json() == {"tree_size": 10, "root_hash": root}

    # Attempt rollback after restart is still refused
    smaller = _make_checkpoint(env["log_seed"], env["log_pub"], tree_size=9, root_hash="99" * 32)
    refused = new_client.post("/cosign", json={"checkpoint": smaller, "consistency_proof": []})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "log_rolled_back"


def test_refuse_invalid_checkpoint_signature(env):
    client = env["client"]
    fake_seed, fake_pub = _keypair()
    # Signed with wrong key
    bad_cp = _make_checkpoint(fake_seed, env["log_pub"], tree_size=1, root_hash="11" * 32)
    resp = client.post("/cosign", json={"checkpoint": bad_cp, "consistency_proof": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "checkpoint_signature_invalid"
