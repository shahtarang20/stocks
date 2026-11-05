"""U6 verification plug-ins (WRITTEN, NOT RUN). Needs Pillow, numpy, scikit-image, imagehash.

U6.1 perceptual passes honest re-renders, fails an altered frame.
U6.2 PSNR/SSIM core fails a truncated or low-quality sequence (ffmpeg path is not exercised here).
Bug re-insertion list: drop block_diff check (altered-frame test fails); drop frame-count compare in
compare/len check (truncation test fails); accept unknown method as passed (registry test fails).
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.services import verify_plugins as vp
from app.services.verification import verify_kit_output

pytest.importorskip("imagehash")
pytest.importorskip("skimage")


def _scene(seed: int = 1, size: int = 256) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size]
    base = np.stack([x * 255 // size, y * 255 // size, (x + y) * 255 // (2 * size)], axis=2).astype(np.float64)
    base[64:128, 64:192] = 40.0  # a shape so the image has structure
    base[160:220, 30:100] = 220.0
    return np.clip(base + rng.normal(0, 1, base.shape), 0, 255).astype(np.uint8)


def _save(arr: np.ndarray, path):
    Image.fromarray(arr).save(path, format="PNG")
    return path


def _manifest(method="perceptual", thresholds=None):
    v = {"method": method}
    if thresholds is not None:
        v["thresholds"] = thresholds
    return {"verification": v}


def test_honest_rerender_passes(tmp_path):
    a = _scene(1)
    noisy = np.clip(a.astype(np.int16) + np.random.default_rng(9).normal(0, 3, a.shape), 0, 255).astype(np.uint8)
    (tmp_path / "r").mkdir()
    r = verify_kit_output(_manifest(), [_save(a, tmp_path / "f1.png")], [_save(noisy, tmp_path / "r" / "f1.png")])
    assert r["status"] == "passed" and r["valid"] is True and r["method"] == "perceptual"


def test_altered_frame_fails(tmp_path):
    a = _scene(1)
    b = a.copy()
    b[100:150, 100:150] = 255 - b[100:150, 100:150]  # a localized edit
    (tmp_path / "r").mkdir()
    r = verify_kit_output(_manifest(), [_save(b, tmp_path / "f1.png")], [_save(a, tmp_path / "r" / "f1.png")])
    assert r["status"] == "failed" and r["valid"] is False


def test_totally_different_frame_fails(tmp_path):
    (tmp_path / "r").mkdir()
    r = verify_kit_output(_manifest(), [_save(_scene(2)[::-1, ::-1].copy(), tmp_path / "f1.png")],
                          [_save(_scene(1), tmp_path / "r" / "f1.png")])
    assert r["status"] == "failed"


def test_size_mismatch_fails_and_missing_reference_is_inconclusive(tmp_path):
    (tmp_path / "r").mkdir()
    p = _save(_scene(1, 256), tmp_path / "f1.png")
    q = _save(_scene(1, 128), tmp_path / "r" / "f1.png")
    assert verify_kit_output(_manifest(), [p], [q])["status"] == "failed"
    assert verify_kit_output(_manifest(), [p], [])["status"] == "inconclusive"


def test_garbage_primary_fails_not_inconclusive(tmp_path):
    (tmp_path / "r").mkdir()
    bad = tmp_path / "f1.png"
    bad.write_bytes(b"not an image")
    r = verify_kit_output(_manifest(), [bad], [_save(_scene(1), tmp_path / "r" / "f1.png")])
    assert r["status"] == "failed"


def test_host_dropping_a_file_fails(tmp_path):
    (tmp_path / "r").mkdir()
    a = _save(_scene(1), tmp_path / "f1.png")
    ref = [_save(_scene(1), tmp_path / "r" / "f1.png"), _save(_scene(3), tmp_path / "r" / "f2.png")]
    assert verify_kit_output(_manifest(), [a], ref)["status"] == "failed"


def test_registry_unknown_method_and_bad_thresholds_are_inconclusive():
    assert vp.run_kit_verification({"verification": {"method": "nope"}}, [], []).status == "inconclusive"
    assert vp.run_kit_verification({}, [], []).status == "inconclusive"
    out = vp.run_kit_verification(_manifest(thresholds={"min_ssim": 0.0}), [], [])
    assert out.status == "inconclusive" and out.reason.startswith("invalid_thresholds")
    out = vp.run_kit_verification(_manifest(thresholds={"bogus": 1}), [], [])
    assert out.reason.startswith("invalid_thresholds")
    assert {"exact", "perceptual", "psnr_ssim"} <= set(vp.methods())


def test_exact_hash(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "a.bin").write_bytes(b"abc")
    (tmp_path / "r" / "a.bin").write_bytes(b"abc")
    m = {"verification": {"method": "exact"}}
    assert vp.run_kit_verification(m, [tmp_path / "a.bin"], [tmp_path / "r" / "a.bin"]).status == "passed"
    (tmp_path / "r" / "a.bin").write_bytes(b"abd")
    assert vp.run_kit_verification(m, [tmp_path / "a.bin"], [tmp_path / "r" / "a.bin"]).status == "failed"


def _clip(n: int, seed=0, noise=0.0):
    rng = np.random.default_rng(seed)
    frames = []
    for i in range(n):
        f = np.tile(np.linspace(0, 255, 160, dtype=np.float64), (90, 1))
        f = np.roll(f, i * 3, axis=1)
        f[20:50, 30:90] = 30 + i
        if noise:
            f = f + rng.normal(0, noise, f.shape)
        frames.append(np.clip(f, 0, 255).astype(np.uint8))
    return frames


def test_psnr_ssim_core_honest_vs_bad():
    from app.services.verify_plugins.psnr_ssim import PsnrSsimPlugin, compare_frame_sequences

    th = PsnrSsimPlugin.default_thresholds
    ref = _clip(20)
    honest = compare_frame_sequences(_clip(20, 1, noise=2.0), ref, th)
    assert honest["mean_psnr"] >= th["min_mean_psnr_db"] and honest["mean_ssim"] >= th["min_mean_ssim"]
    starved = compare_frame_sequences(_clip(20, 2, noise=40.0), ref, th)
    assert starved["mean_psnr"] < th["min_mean_psnr_db"]


def test_psnr_plugin_without_reference_is_inconclusive(tmp_path):
    p = tmp_path / "a.mp4"
    p.write_bytes(b"x")
    assert vp.run_kit_verification(_manifest("psnr_ssim"), [p], []).status == "inconclusive"


# ---- review-all: host-controlled files can never turn a check into "inconclusive" or exhaust memory ----
def test_cosine_wer_loss_unreadable_primary_fails_not_inconclusive(tmp_path):
    np = pytest.importorskip("numpy")
    ref, bad = tmp_path / "ref", tmp_path / "bad"
    ref.mkdir(); bad.mkdir()
    np.save(ref / "embeddings.npy", np.ones((2, 3), dtype=np.float32))
    (bad / "embeddings.npy").write_bytes(b"\x93NUMPY\x01\x00garbage")
    out = vp.get_plugin("cosine").verify([bad / "embeddings.npy"], [ref / "embeddings.npy"], {})
    assert out.status == "failed", out
    # wer: the host's transcript is deeply nested JSON (RecursionError) -> failed, not an uncaught exception
    (ref / "t.json").write_text('{"segments":[{"text":"hello world"}]}')
    (bad / "t.json").write_text("[" * 200000)
    out = vp.get_plugin("wer").verify([bad / "t.json"], [ref / "t.json"], {})
    assert out.status == "failed", out
    # loss_curve
    (ref / "metrics.json").write_text('{"final_loss": 1.0}')
    (bad / "metrics.json").write_text("[" * 200000)
    out = vp.get_plugin("loss_curve").verify([bad / "metrics.json"], [ref / "metrics.json"], {})
    assert out.status == "failed", out
    # an unreadable REFERENCE stays inconclusive
    out = vp.get_plugin("loss_curve").verify([ref / "metrics.json"], [bad / "metrics.json"], {})
    assert out.status == "inconclusive", out


def test_audio_spectral_sample_cap(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    import wave
    from app.services.verify_plugins import audio_spectral as au

    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\x00\x01" * 2 * 4000)
    monkeypatch.setattr(au, "MAX_SAMPLES", 100)
    with pytest.raises(au._Unreadable):
        au.read_wav(p)


def test_perceptual_banded_diff_matches_naive():
    np = pytest.importorskip("numpy")
    from app.services.verify_plugins.perceptual import diff_stats

    rng = np.random.default_rng(7)
    a = rng.integers(0, 256, (37, 53, 3), dtype=np.uint8)
    b = rng.integers(0, 256, (37, 53, 3), dtype=np.uint8)
    mad, blk = diff_stats(a, b)
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    assert mad == pytest.approx(d.mean() / 255.0)
    assert 0.0 < blk <= 1.0
    assert diff_stats(a, a) == (0.0, 0.0)
