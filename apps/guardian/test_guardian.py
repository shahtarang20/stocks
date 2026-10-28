"""External guardian (WP4, checkpoint 4.2): every refusal rule, plus the log rules that make
log-before-release real (signed checkpoints, consistency with what the guardian saw before)."""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from fastapi.testclient import TestClient

import apps.guardian.main as guardian_app
from apps.guardian.config import GuardianConfig
from apps.guardian.crypto import (
    CHECKPOINT_FIELDS,
    canonical_json,
    hash_internal_node,
    hash_leaf,
    open_sealed,
    release_aad,
    seal,
    share_aad,
    x25519_public_from_private,
)

GUARDIAN = "partner"
VERDICT_FIELDS = (
    "verdict_id", "job_id", "machine_id", "image_hash", "tier_reached", "ephemeral_public_key",
    "receipt_signing_public_key", "nonce", "issued_at", "expires_at", "verifier_public_key",
)
IMAGE = "ab" * 32


def _ed():
    k = ed25519.Ed25519PrivateKey.generate()
    return k, k.public_key().public_bytes_raw().hex()


# A tiny RFC 6962 log, built independently of the API, to produce checkpoints and proofs.
def _mth(leaves):
    if len(leaves) == 1:
        return leaves[0]
    k = 1
    while k << 1 < len(leaves):
        k <<= 1
    return hash_internal_node(_mth(leaves[:k]), _mth(leaves[k:]))


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


class World:
    def __init__(self, tmp_path):
        self.verifier, self.verifier_pub = _ed()
        self.logkey, self.log_pub = _ed()
        self.g_priv = "01" * 32
        self.g_pub = x25519_public_from_private(self.g_priv)
        guardian_app.config = GuardianConfig(
            name=GUARDIAN, private_key=self.g_priv, verifier_public_key=self.verifier_pub,
            log_public_key=self.log_pub, trusted_images={IMAGE}, state_path=str(tmp_path / "state.json"),
            max_releases_per_job=3,
        )
        self.client = TestClient(guardian_app.app)
        self.leaves: list[str] = []
        self.entries: list[dict] = []

    def verdict(self, vid="v-1", machine="m-1", tier="sealed", image=IMAGE, expires_in=600, key=None, job="job-1"):
        host = x25519.X25519PrivateKey.generate()
        now = dt.datetime.now(dt.timezone.utc)
        v = {
            "verdict_id": vid, "job_id": job, "machine_id": machine, "image_hash": image, "tier_reached": tier,
            "ephemeral_public_key": host.public_key().public_bytes_raw().hex(), "receipt_signing_public_key": "04" * 32,
            "nonce": "n" * 16, "issued_at": now.isoformat(), "expires_at": (now + dt.timedelta(seconds=expires_in)).isoformat(),
            "verifier_public_key": self.verifier_pub,
        }
        v["signature"] = (key or self.verifier).sign(canonical_json({f: v[f] for f in VERDICT_FIELDS})).hex()
        return v, host

    def log(self, entry):
        self.entries.append(entry)
        self.leaves.append(hash_leaf("KEY_RELEASE", entry))
        return len(self.leaves) - 1

    def checkpoint(self, size=None, key=None):
        size = size or len(self.leaves)
        cp = {"v": "ccpp-log-v1", "tree_size": size, "root_hash": _mth(self.leaves[:size]), "timestamp": "t", "log_public_key": self.log_pub}
        cp["signature"] = (key or self.logkey).sign(canonical_json({f: cp[f] for f in CHECKPOINT_FIELDS})).hex()
        return cp

    def request(self, job="job-1", vid="v-1", machine="m-1", verdict=None, entry_over=None, cp=None, from_size=None):
        if verdict is None:
            verdict, _ = self.verdict(vid=vid, machine=machine, job=job)
        entry = {"guardian": GUARDIAN, "job_id": job, "machine_id": machine, "verdict_id": verdict["verdict_id"], "share_index": 2,
                 "released_at": "t"}
        entry.update(entry_over or {})
        idx = self.log(entry)
        cp = cp or self.checkpoint()
        seen = from_size if from_size is not None else guardian_app._load_state()["tree_size"]
        consistency = _subproof(seen, self.leaves[: cp["tree_size"]], True) if 0 < seen < cp["tree_size"] else []
        return {
            "job_id": job, "machine_id": machine, "attestation_verdict": verdict,
            "sealed_share": {"x": 2, "envelope": seal(self.g_pub, b"S" * 32, share_aad(GUARDIAN))},
            "log_entry": entry,
            "inclusion_proof": {"leaf_index": idx, "tree_size": cp["tree_size"], "audit_path": _path(idx, self.leaves[: cp["tree_size"]])},
            "checkpoint": cp, "consistency_proof": consistency,
        }


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_release_to_attested_host_only(world):
    verdict, host = world.verdict()
    r = world.client.post("/release", json=world.request(verdict=verdict))
    assert r.status_code == 200, r.text
    wrapped = r.json()["wrapped_share"]
    host_priv = host.private_bytes_raw().hex()
    assert open_sealed(host_priv, wrapped, release_aad("job-1", GUARDIAN)) == b"S" * 32
    with pytest.raises(Exception):
        open_sealed("02" * 32, wrapped, release_aad("job-1", GUARDIAN))


def test_caller_supplied_root_is_not_trusted(world):
    """The old flaw: a one-leaf 'log' invented by the caller. Now the root must be signed by the log key."""
    req = world.request()
    fake_key, _ = _ed()
    req["checkpoint"] = world.checkpoint(key=fake_key)
    assert world.client.post("/release", json=req).json()["detail"] == "checkpoint_signature_invalid"


def test_log_cannot_be_rewritten_or_rolled_back(world):
    assert world.client.post("/release", json=world.request(job="a", vid="va")).status_code == 200
    seen = guardian_app._load_state()["tree_size"]
    # A forked log: same size history replaced by different entries.
    world.leaves = [hash_leaf("KEY_RELEASE", {"forged": i}) for i in range(seen)]
    forked = world.request(job="b", vid="vb", from_size=seen)
    assert world.client.post("/release", json=forked).json()["detail"] == "log_inconsistent"
    # A smaller log than the guardian already saw.
    small = world.request(job="c", vid="vc")
    small["checkpoint"] = world.checkpoint(size=1)
    small["inclusion_proof"]["tree_size"] = 1
    assert world.client.post("/release", json=small).status_code in (400, 409)


def test_honest_growth_is_accepted_with_a_consistency_proof(world):
    for i in range(5):
        r = world.client.post("/release", json=world.request(job=f"job-{i}", vid=f"v{i}"))
        assert r.status_code == 200, r.text
    assert guardian_app._load_state()["tree_size"] == 5


@pytest.mark.parametrize(
    "case,code",
    [
        ("forged_verdict", "verdict_signature_invalid"),
        ("expired", "verdict_expired"),
        ("other_machine", "machine_id_mismatch"),
        ("other_job", "verdict_not_for_this_job"),
        ("machine_level", "verdict_not_for_this_job"),
        ("v1_no_job_id", "verdict_signature_invalid"),
        ("open_tier", "tier_not_private"),
        ("untrusted_image", "image_not_trusted"),
        ("entry_other_job", "log_entry_mismatch"),
        ("entry_other_share", "log_entry_mismatch"),
        ("bad_inclusion", "inclusion_proof_invalid"),
    ],
)
def test_refusals(world, case, code):
    other, _ = _ed()
    if case == "forged_verdict":
        req = world.request(verdict=world.verdict(key=other)[0])
    elif case == "expired":
        req = world.request(verdict=world.verdict(expires_in=-10)[0])
    elif case == "other_machine":
        req = world.request(verdict=world.verdict(machine="m-2")[0])
    elif case == "other_job":  # B6: a verdict attested for job-2 cannot release job-1's share
        req = world.request(job="job-1", verdict=world.verdict(job="job-2")[0])
    elif case == "machine_level":  # a machine-level verdict (job_id null) releases nothing
        req = world.request(verdict=world.verdict(job=None)[0])
    elif case == "v1_no_job_id":  # a correctly signed v1 verdict (no job_id field) is refused
        v, _ = world.verdict()
        v.pop("job_id")
        v["signature"] = world.verifier.sign(canonical_json({f: v[f] for f in VERDICT_FIELDS if f != "job_id"})).hex()
        req = world.request(verdict=v)
    elif case == "open_tier":
        req = world.request(verdict=world.verdict(tier="open")[0])
    elif case == "untrusted_image":
        req = world.request(verdict=world.verdict(image="cd" * 32)[0])
    elif case == "entry_other_job":
        req = world.request(entry_over={"job_id": "job-X"})
    elif case == "entry_other_share":
        req = world.request(entry_over={"share_index": 3})
    else:
        req = world.request()
        req["inclusion_proof"]["leaf_index"] = (req["inclusion_proof"]["leaf_index"] + 1) % 2
        req["inclusion_proof"]["audit_path"] = ["00" * 32]
    assert world.client.post("/release", json=req).json()["detail"] == code


def test_replay_rules_survive_restart(world):
    verdict, _ = world.verdict(vid="v-1")
    assert world.client.post("/release", json=world.request(verdict=verdict)).status_code == 200
    # Simulate a restart: the in-memory module state is gone, the file is not.
    world.client = TestClient(guardian_app.app)
    r = world.client.post("/release", json=world.request(verdict=verdict))
    assert r.json()["detail"] == "key_already_released_for_verdict"
    # A retry on another host with a NEW verdict is allowed, up to the limit.
    assert world.client.post("/release", json=world.request(vid="v-2")).status_code == 200
    assert world.client.post("/release", json=world.request(vid="v-3")).status_code == 200
    assert world.client.post("/release", json=world.request(vid="v-4")).json()["detail"] == "release_limit_reached_for_job"


def test_incomplete_configuration_releases_nothing(world, tmp_path):
    guardian_app.config = GuardianConfig(name=GUARDIAN, private_key="01" * 32, verifier_public_key=world.verifier_pub,
                                         log_public_key=world.log_pub, trusted_images=set(), state_path=str(tmp_path / "s.json"))
    r = world.client.post("/release", json=world.request())
    assert r.status_code == 503 and r.json()["detail"] == "guardian_not_configured"
    assert "TRUSTED_IMAGES" in world.client.get("/health").json()["missing"]


def test_guardian_crypto_passes_the_shared_ccpp_vectors():
    """The guardian has its own copy of the crypto; it must agree with the published vectors."""
    import json
    import pathlib

    from apps.guardian.crypto import verify_signature

    v = json.loads((pathlib.Path(__file__).resolve().parents[2] / "docs/specs/ccpp-v1-vectors.json").read_text())
    s = v["sealing"]
    assert open_sealed(s["recipient_private_hex"], s["envelope"], bytes.fromhex(s["aad_hex"])) == bytes.fromhex(s["plaintext_hex"])
    e = v["ed25519"]
    assert verify_signature(e["expected_public_key_hex"], bytes.fromhex(e["message_hex"]), e["expected_signature_hex"])
    assert x25519_public_from_private(v["x25519"]["private_key_hex"]) == v["x25519"]["expected_public_key_hex"]


def test_guardian_witness_threshold_enforcement(world):
    """E5: Guardian refuses checkpoints below the witness threshold."""
    wit_priv, wit_pub = _ed()
    guardian_app.config.witness_keys = {"witness-alpha": wit_pub}
    guardian_app.config.witness_threshold = 1

    # Without cosignatures -> refused
    req = world.request(job="job-wit-1", vid="v-wit-1")
    res1 = world.client.post("/release", json=req)
    assert res1.status_code == 400
    assert res1.json()["detail"] == "witness_cosignatures_insufficient"

    # With valid cosignature -> accepted
    cp = req["checkpoint"]
    stmt = {
        "v": "ccpp-witness-v1",
        "tree_size": cp["tree_size"],
        "root_hash": cp["root_hash"].lower(),
        "log_public_key": guardian_app.config.log_public_key.lower(),
        "witness": "witness-alpha",
    }
    sig = wit_priv.sign(canonical_json(stmt)).hex()
    req["witness_cosignatures"] = [{
        "witness": "witness-alpha",
        "signature": sig,
        "tree_size": cp["tree_size"],
        "root_hash": cp["root_hash"],
    }]

    res2 = world.client.post("/release", json=req)
    assert res2.status_code == 200


def _cosign(world, req, name, key):
    cp = req["checkpoint"]
    stmt = {"v": "ccpp-witness-v1", "tree_size": cp["tree_size"], "root_hash": cp["root_hash"].lower(),
            "log_public_key": guardian_app.config.log_public_key.lower(), "witness": name}
    return {"witness": name, "signature": key.sign(canonical_json(stmt)).hex()}


def test_one_witness_key_under_two_names_counts_once(world):
    """A threshold of 2 means two independent witnesses, not one key listed twice."""
    key, pub = _ed()
    other_key, other_pub = _ed()
    guardian_app.config.witness_keys = {"a": pub, "b": pub, "c": other_pub}  # two distinct keys: config is valid
    guardian_app.config.witness_threshold = 2
    req = world.request(job="job-dup", vid="v-dup")
    req["witness_cosignatures"] = [_cosign(world, req, "a", key), _cosign(world, req, "b", key)]
    assert world.client.post("/release", json=req).json()["detail"] == "witness_cosignatures_insufficient"
    req["witness_cosignatures"].append(_cosign(world, req, "c", other_key))
    assert world.client.post("/release", json=req).status_code == 200


def test_an_unreachable_witness_threshold_is_reported_as_misconfigured(world):
    _, pub = _ed()
    guardian_app.config.witness_keys = {"a": pub}
    guardian_app.config.witness_threshold = 2
    assert "GUARDIAN_WITNESS_KEYS" in world.client.get("/health").json()["missing"]
    r = world.client.post("/release", json=world.request(job="job-mis", vid="v-mis"))
    assert r.status_code == 503 and r.json()["detail"] == "guardian_not_configured"
