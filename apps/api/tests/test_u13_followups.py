"""U13 follow-ups and kit verification plug-ins wer / cosine / loss_curve (written, not run).

Bug re-insertion list: drop the WER threshold compare (high-WER test fails); drop the ids order check (ids test fails);
treat a missing reference as failed (inconclusive tests fail); drop the multi-node tier refusal (tier test fails).
"""

from __future__ import annotations

import json

import pytest

from app.services import verify_plugins as vp
from app.services.verify_plugins.base import FAILED, INCONCLUSIVE, PASSED


def _m(method, th=None):
    v = {"method": method}
    if th is not None:
        v["thresholds"] = th
    return {"verification": v}


def test_registered():
    assert {"wer", "cosine", "loss_curve"} <= set(vp.methods())


def test_wer(tmp_path):
    a, b = tmp_path / "p", tmp_path / "r"
    a.mkdir(), b.mkdir()
    (b / "transcript.txt").write_text("the quick brown fox jumps over the lazy dog\n")
    (a / "transcript.txt").write_text("The quick brown fox jumps over the lazy dog.\n")
    assert vp.run_kit_verification(_m("wer"), [a / "transcript.txt"], [b / "transcript.txt"]).status == PASSED
    (a / "transcript.txt").write_text("totally different words here now please thanks\n")
    assert vp.run_kit_verification(_m("wer"), [a / "transcript.txt"], [b / "transcript.txt"]).status == FAILED
    assert vp.run_kit_verification(_m("wer"), [a / "transcript.txt"], []).status == INCONCLUSIVE
    assert vp.run_kit_verification(_m("wer", {"max_wer": 5}), [a / "transcript.txt"], [b / "transcript.txt"]).status == INCONCLUSIVE


def test_wer_srt_and_json(tmp_path):
    # Files are paired by BASENAME (see verify_plugins/wer.py), so primary and reference use the same name.
    a, b = tmp_path / "p", tmp_path / "r"
    a.mkdir(), b.mkdir()
    (a / "t.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello world\n\n")
    (b / "t.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhello world\n\n")
    assert vp.run_kit_verification(_m("wer"), [a / "t.srt"], [b / "t.srt"]).status == PASSED


def test_cosine(tmp_path):
    np = pytest.importorskip("numpy")
    a, b = tmp_path / "p", tmp_path / "r"
    a.mkdir(), b.mkdir()
    m = np.random.default_rng(1).normal(size=(10, 8)).astype("float32")
    np.save(b / "embeddings.npy", m)
    (b / "ids.json").write_text(json.dumps(list(range(10))))
    np.save(a / "embeddings.npy", m + 1e-4)
    (a / "ids.json").write_text(json.dumps(list(range(10))))
    pf, rf = [a / "embeddings.npy", a / "ids.json"], [b / "embeddings.npy", b / "ids.json"]
    assert vp.run_kit_verification(_m("cosine"), pf, rf).status == PASSED
    bad = m.copy()
    bad[3] = -bad[3]
    np.save(a / "embeddings.npy", bad)
    assert vp.run_kit_verification(_m("cosine"), pf, rf).status == FAILED
    np.save(a / "embeddings.npy", m)
    (a / "ids.json").write_text(json.dumps(list(reversed(range(10)))))
    assert vp.run_kit_verification(_m("cosine"), pf, rf).status == FAILED
    assert vp.run_kit_verification(_m("cosine"), pf, []).status == INCONCLUSIVE


def test_loss_curve(tmp_path):
    (tmp_path / "p").mkdir(), (tmp_path / "r").mkdir()
    p, r = tmp_path / "p" / "metrics.json", tmp_path / "r" / "metrics.json"
    r.write_text(json.dumps({"final_loss": 1.0, "finite": True}))
    p.write_text(json.dumps({"final_loss": 1.1, "finite": True}))
    assert vp.run_kit_verification(_m("loss_curve"), [p], [r]).status == PASSED
    p.write_text(json.dumps({"final_loss": 2.0, "finite": True}))
    assert vp.run_kit_verification(_m("loss_curve"), [p], [r]).status == FAILED
    p.write_text(json.dumps({"final_loss": None, "finite": False}))
    assert vp.run_kit_verification(_m("loss_curve"), [p], [r]).status == FAILED
    p.write_text(json.dumps({"final_loss": 1.0}))
    r.write_text("{}")
    assert vp.run_kit_verification(_m("loss_curve"), [p], [r]).status == INCONCLUSIVE
