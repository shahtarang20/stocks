"""Kits ai-video, text-to-speech, scientific-compute and their verification plug-ins (WRITTEN, NOT RUN).
Run from repo root: pytest kits/tests. Pure Python (numpy for the plug-in tests); no torch, GPU, docker or ffmpeg.
Bug re-insertion list: drop the '..' check in scientific-compute safe_zip_members (zip test fails); drop the flag check in
ExactDeterministicPlugin (inconclusive test fails); drop the sample-rate check in AudioSpectralPlugin (rate test fails);
drop the symlink removal in sanitize_results (symlink test fails); change the kit.json method (registered-method test fails).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import wave
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KITS = ROOT / "kits"
sys.path.insert(0, str(KITS / "_common"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "apps" / "api"))

import kit_ci  # noqa: E402
import kitrt  # noqa: E402

SLUGS = ["ai-video", "text-to-speech", "scientific-compute"]
METHODS = {"ai-video": "psnr_ssim", "text-to-speech": "audio_spectral", "scientific-compute": "exact_deterministic"}


def _load(slug):
    spec = importlib.util.spec_from_file_location("ep4_" + slug.replace("-", "_"), KITS / slug / "entrypoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("slug", SLUGS)
def test_offline_gate_schema_defaults_and_digest_placeholder(slug):
    assert kit_ci.check_kit(KITS / slug) == []
    m = json.loads((KITS / slug / "kit.json").read_text(encoding="utf-8"))
    assert m["image"].endswith("@sha256:PENDING")
    schema = m["param_schema"]
    sample = {"ai-video": {"prompts": ["x"]}, "text-to-speech": {"texts": ["x"]}, "scientific-compute": {}}[slug]
    out = kitrt.validate(sample, schema)
    for k, sub in schema["properties"].items():
        if "default" in sub:
            assert out[k] == sub["default"]
    with pytest.raises(kitrt.KitError):
        kitrt.validate({**sample, "bogus": 1}, schema)


@pytest.mark.parametrize("slug", SLUGS)
def test_dockerfile_rules_and_verification_record(slug):
    text = (KITS / slug / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001" in text and "must be pinned by digest" in text
    v = json.loads((KITS / slug / "kit.json").read_text(encoding="utf-8"))["verification"]
    assert v["method"] == METHODS[slug] and "OWNER DECISION" in v["owner_decision"]
    if slug != "scientific-compute":
        assert 'test "${' in text and "PENDING" in text  # weights pins fail the build while PENDING


@pytest.mark.parametrize("slug", SLUGS)
def test_verification_method_is_registered_and_thresholds_valid(slug):
    pytest.importorskip("numpy")
    from app.services import verify_plugins as vp
    from app.services.verify_plugins.base import resolve_thresholds

    m = json.loads((KITS / slug / "kit.json").read_text(encoding="utf-8"))
    plugin = vp.get_plugin(m["verification"]["method"])
    assert plugin is not None
    resolve_thresholds(plugin.default_thresholds, plugin.threshold_bounds, m["verification"]["thresholds"])


def test_ai_video_plan_and_ffmpeg_command():
    ep = _load("ai-video")
    items = ep.plan({"prompts": ["a", "b", "c"], "seed": 4294967295})
    assert [i["seed"] for i in items] == [4294967295, 0, 1] and items[2]["file"] == "video_00002.mp4"
    cmd = ep.ffmpeg_cmd("ffmpeg", 480, 320, 8, 18, "/o.mp4")
    assert isinstance(cmd, list) and cmd[cmd.index("-s") + 1] == "480x320" and cmd[cmd.index("-threads") + 1] == "1"
    assert "-bitexact" not in cmd and "+bitexact" in cmd and cmd[cmd.index("-map_metadata") + 1] == "-1"
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "pipe,file" and cmd[-1] == "/o.mp4"


def test_tts_plan_language_and_wav(tmp_path):
    np = pytest.importorskip("numpy")
    ep = _load("text-to-speech")
    assert [i["file"] for i in ep.plan({"texts": ["a", "b"]})] == ["speech_00000.wav", "speech_00001.wav"]
    assert ep.lang_code("bf_emma") == "b" and ep.lang_code("af_heart") == "a"
    with pytest.raises(kitrt.KitError):
        ep.lang_code("zz_x")
    assert ep.pcm16(np.array([0.0, 1.0, -1.0, 2.0])) == np.array([0, 32767, -32767, 32767], dtype="<i2").tobytes()
    p = tmp_path / "a.wav"
    ep.write_wav(str(p), np.zeros(240))
    ep.write_wav(str(tmp_path / "b.wav"), np.zeros(240))
    assert p.read_bytes() == (tmp_path / "b.wav").read_bytes()  # no timestamps: a pure function of the samples
    with wave.open(str(p)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)
    with pytest.raises(kitrt.KitError):
        ep.write_wav(str(tmp_path / "c.wav"), np.zeros(0))


def test_scientific_zip_safety_env_argv(tmp_path):
    ep = _load("scientific-compute")
    good = tmp_path / "g.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("a.csv", b"1")
    with zipfile.ZipFile(good) as z:
        assert [i.filename for i in ep.safe_zip_members(z)] == ["a.csv"]
    for bad in ("../x", "/abs", "a/../../x", "a\\b"):
        p = tmp_path / "b.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(zipfile.ZipInfo(bad), b"x")
        with zipfile.ZipFile(p) as z, pytest.raises(kitrt.KitError):
            ep.safe_zip_members(z)
    link = tmp_path / "l.zip"
    info = zipfile.ZipInfo("lnk")
    info.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(link, "w") as z:
        z.writestr(info, "target")
    with zipfile.ZipFile(link) as z, pytest.raises(kitrt.KitError):
        ep.safe_zip_members(z)
    params = {"deterministic": True, "seed": 5, "args": ["--n", "3"]}
    env = ep.build_env(params, "/w", "/w/data", "/out/results")
    assert env["PYTHONHASHSEED"] == "5" and env["OMP_NUM_THREADS"] == "1" and env["TZ"] == "UTC" and env["KIT_SEED"] == "5"
    assert "PYTHONPATH" not in env and "COMPUTECOIN_INPUT" not in env
    assert ep.build_env({**params, "deterministic": False}, "/w", "/d", "/r")["PYTHONHASHSEED"] == "random"
    argv = ep.build_argv("/w/script.py", ["--n", "3"])
    assert argv[1:4] == ["-I", "-B", "/w/script.py"] and argv[4:] == ["--n", "3"]


def test_scientific_sanitize_results_and_tail(tmp_path):
    ep = _load("scientific-compute")
    (tmp_path / "ok.txt").write_text("hi")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    os.symlink("/etc/passwd", tmp_path / "evil")
    os.symlink("/etc", tmp_path / "evildir")
    assert ep.sanitize_results(str(tmp_path), 1024) == ["ok.txt", os.path.join("sub", "b.txt")]
    assert not os.path.lexists(tmp_path / "evil") and not os.path.lexists(tmp_path / "evildir")
    (tmp_path / "big.bin").write_bytes(b"x" * 2000)
    with pytest.raises(kitrt.KitError):
        ep.sanitize_results(str(tmp_path), 1024)
    log = tmp_path / "log.txt"
    log.write_bytes(b"a" * 100 + b"END")
    ep.tail_file(str(log), 10)
    assert log.read_bytes() == b"a" * 7 + b"END"


# ---- verification plug-ins --------------------------------------------------------------------------------------------

def _wav(path, samples, rate=24000):
    import numpy as np

    pcm = np.rint(np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return Path(path)


def _tone(freq, seconds=1.0, rate=24000, amp=0.4):
    import numpy as np

    t = np.arange(int(rate * seconds)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def test_audio_spectral_plugin(tmp_path):
    np = pytest.importorskip("numpy")
    from app.services.verify_plugins.audio_spectral import AudioSpectralPlugin

    pl = AudioSpectralPlugin()
    (tmp_path / "p").mkdir()
    (tmp_path / "r").mkdir()
    ref = _wav(tmp_path / "r" / "s.wav", _tone(440))
    same = _wav(tmp_path / "p" / "s.wav", _tone(440) + 1e-4)
    assert pl.verify([same], [ref]).status == "passed"
    # missing reference: inconclusive; missing primary: failed
    assert pl.verify([same], []).status == "inconclusive"
    assert pl.verify([], [ref]).status == "failed"
    # different content fails
    other = _wav(tmp_path / "p" / "s.wav", _tone(3000))
    assert pl.verify([other], [ref]).status == "failed"
    # different duration fails
    longer = _wav(tmp_path / "p" / "s.wav", _tone(440, seconds=1.6))
    assert pl.verify([longer], [ref]).reason == "spectral_mismatch"
    # different sample rate fails
    rate = _wav(tmp_path / "p" / "s.wav", _tone(440, rate=16000), rate=16000)
    assert pl.verify([rate], [ref]).reason == "sample_rate_mismatch"
    # garbage primary fails, garbage reference is inconclusive
    junk = tmp_path / "p" / "s.wav"
    junk.write_bytes(b"not a wav")
    assert pl.verify([junk], [ref]).status == "failed"
    junk_ref = tmp_path / "r" / "s.wav"
    junk_ref.write_bytes(b"not a wav")
    assert pl.verify([same if same.exists() else junk], [junk_ref]).status == "inconclusive"
    # invalid thresholds are inconclusive
    assert pl.verify([same], [ref], {"min_cosine": 5.0}).status == "inconclusive"


def test_exact_deterministic_plugin(tmp_path):
    from app.services.verify_plugins.exact_deterministic import ExactDeterministicPlugin

    pl = ExactDeterministicPlugin()

    def side(name, flag, body):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        (d / "determinism.json").write_text(json.dumps({"deterministic": flag}))
        (d / "out.txt").write_text(body)
        return [d / "determinism.json", d / "out.txt"]

    assert pl.verify(side("p1", True, "x"), side("r1", True, "x")).status == "passed"
    assert pl.verify(side("p2", True, "y"), side("r2", True, "x")).status == "failed"
    nd = pl.verify(side("p3", False, "y"), side("r3", False, "x"))
    assert nd.status == "inconclusive" and nd.reason == "kit_not_deterministic"
    # a host that flips its own flag cannot dodge a deterministic reference
    assert pl.verify(side("p4", False, "x"), side("r4", True, "x")).status == "failed"
    assert pl.verify(side("p5", True, "x"), []).status == "inconclusive"
    missing = tmp_path / "m"
    missing.mkdir()
    (missing / "out.txt").write_text("x")
    assert pl.verify([missing / "out.txt"], [missing / "out.txt"]).reason == "determinism_marker_missing"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "determinism.json").write_text("{not json")
    assert pl.verify([bad / "determinism.json"], [bad / "determinism.json"]).status == "inconclusive"
