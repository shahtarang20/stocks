"""ccpp-fanout-v1 vectors (docs/specs/ccpp-v1.md 6g): the committed JSON must equal what scripts/gen_fanout_vectors.py computes.

Skipped with a clear reason until the owner has run ``python scripts/gen_fanout_vectors.py --write`` (the section ships as
UNVERIFIED / NOT GENERATED: ``expected`` is null; nothing was computed by hand).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "gen_fanout_vectors.py"
VECTORS = ROOT / "docs" / "specs" / "ccpp-v1-vectors.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("gen_fanout_vectors", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # defines functions only; main() is behind __main__
    return mod


def _section() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8")).get("fanout_v1") or {}


def _require_generated(sec: dict) -> None:
    if sec.get("expected") is None or sec.get("inputs") is None:
        pytest.skip("fanout_v1 is UNVERIFIED / NOT GENERATED: run `python scripts/gen_fanout_vectors.py --write` first")


def test_generator_is_deterministic():
    g = _load_script()
    assert json.dumps(g.section(), sort_keys=True) == json.dumps(g.section(), sort_keys=True)


def test_generator_matches_app_implementation():
    """Independent script vs apps/api fanout_privacy (does not need the JSON to be populated)."""
    g = _load_script()
    from app.services import fanout_privacy as fp
    from app.services import privacy_crypto as pc

    sec = json.loads(json.dumps(g.section()))
    exp = sec["expected"]
    hashes_ = [fp.part_receipt_hash(b) for b in g.INPUTS["part_receipt_bodies"]]
    assert hashes_ == exp["part_receipt_hashes"]
    leaves = [fp.part_leaf(n, g.INPUTS["part_job_ids"][n], hashes_[n]) for n in range(3)]
    assert leaves == exp["part_leaf_hashes"]
    assert fp.compute_merkle_root(leaves) == exp["merkle_root"]
    assert fp._signing_bytes(exp["job_receipt"]).decode() == exp["signing_bytes_utf8"]
    assert fp.receipt_log_payload(exp["job_receipt"]) == exp["transparency_log"]["payload"]
    assert fp.verify_job_receipt(exp["job_receipt"], verifier_public_hex=exp["verifier_public_key_hex"])["valid"]
    s = g.INPUTS["stream"]
    ch = exp["stream_chunk"]
    assert pc.stream_aad(s["job_id"], s["seq"], s["final"]).decode() == ch["aad_utf8"]
    assert pc.open_sealed(s["recipient_private_hex"], ch["envelope"], ch["aad_utf8"].encode()).hex() == ch["padded_plaintext_hex"]


def test_committed_vectors_equal_regenerated():
    sec = _section()
    _require_generated(sec)
    g = _load_script()
    assert sec == json.loads(json.dumps(g.section())), "vectors file differs from the generator: run --write and review the diff"


def test_committed_inputs_are_the_scripts_inputs():
    sec = _section()
    _require_generated(sec)
    g = _load_script()
    assert sec["inputs"] == json.loads(json.dumps(g.INPUTS))
