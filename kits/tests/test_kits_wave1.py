"""Wave-1 Kit checks (WRITTEN, NOT RUN). Run from repo root: pytest kits/tests
Pure-Python only: no docker, GPU, torch or Blender needed. Entrypoints import their heavy deps lazily.
Bug re-insertion list: set a kit image to a made-up digest in kit.json (placeholder test fails); drop USER 10001
(dockerfile test fails); make params.schema.json differ (schema-sync test fails); remove the COMMIT hash check
(commit test fails); allow '..' zip names (zip test fails).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
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

SLUGS = ["ai-image", "blender-render", "ffmpeg-transcode", "upscale"]
FAKE_DIGEST = "sha256:" + "ab" * 32  # test-only value, never used for a real Kit


def _load(slug: str):
    spec = importlib.util.spec_from_file_location("ep_" + slug.replace("-", "_"), KITS / slug / "entrypoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest(slug):
    return json.loads((KITS / slug / "kit.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("slug", SLUGS)
def test_kit_passes_offline_gate(slug):
    assert kit_ci.check_kit(KITS / slug) == []


@pytest.mark.parametrize("slug", SLUGS)
def test_placeholder_image_fails_registry_validation_and_real_digest_passes(slug):
    pytest.importorskip("jsonschema")
    from app.services.kits import validate_manifest

    m = _manifest(slug)
    errs = validate_manifest(m)
    assert any(e["code"] == "image_must_be_pinned_by_sha256_digest" for e in errs)  # PENDING must fail
    filled = copy.deepcopy(m)
    filled["image"] = m["image"].split("@")[0] + "@" + FAKE_DIGEST
    assert validate_manifest(filled) == []  # the only blocker was the placeholder


@pytest.mark.parametrize("slug", SLUGS)
def test_verification_methods_are_registered(slug):
    pytest.importorskip("numpy")
    from app.services import verify_plugins

    assert _manifest(slug)["verification"]["method"] in verify_plugins.methods()


def test_param_schema_defaults_validate_through_runtime_validator():
    for slug in SLUGS:
        schema = json.loads((KITS / slug / "params.schema.json").read_text())
        sample = {"ai-image": {"prompts": ["x"]}, "blender-render": {"frame_start": 1, "frame_end": 2}}.get(slug, {})
        out = kitrt.validate(sample, schema)
        for k, sub in schema["properties"].items():
            if "default" in sub:
                assert out[k] == sub["default"]
        with pytest.raises(kitrt.KitError):
            kitrt.validate({**sample, "bogus": 1}, schema)


def test_blender_frames_and_command():
    ep = _load("blender-render")
    assert ep.frame_list({"frame_start": 1, "frame_end": 10, "frame_step": 3}) == [1, 4, 7, 10]
    with pytest.raises(kitrt.KitError):
        ep.frame_list({"frame_start": 5, "frame_end": 1, "frame_step": 1})
    cmd = ep.build_cmd("/w/scene.blend", 4, "/w/render_####", "/w/setup.json")
    assert "--disable-autoexec" in cmd and cmd[cmd.index("-f") + 1] == "4" and cmd[-2:] == ["--", "/w/setup.json"]


def test_ffmpeg_args_nvenc_cpu_and_safety():
    ep = _load("ffmpeg-transcode")
    p = {"codec": "h264", "encoder": "auto", "crf": 20, "preset": "slow", "audio": "aac", "start_seconds": 2.0,
         "duration_seconds": 5.0, "height": 720}
    nv = ep.build_args(p, "/in/source", "/out/result.mp4", "nvenc")
    assert "h264_nvenc" in nv and nv[nv.index("-cq") + 1] == "20" and nv[nv.index("-preset") + 1] == "p6"
    cpu = ep.build_args(p, "/in/source", "/out/result.mp4", "cpu")
    assert "libx264" in cpu and cpu[cpu.index("-crf") + 1] == "20"
    for a in (nv, cpu):
        assert a[a.index("-protocol_whitelist") + 1] == "file,pipe"
        assert a.index("-ss") < a.index("-i") < a.index("-t")
    assert "-an" in ep.build_args({**p, "audio": "none"}, "/in/source", "/out/r.mp4", "cpu")


def test_ai_image_plan_seeds():
    ep = _load("ai-image")
    items = ep.plan({"prompts": ["a", "b", "c"], "seed": 4294967295})
    assert [i["seed"] for i in items] == [4294967295, 0, 1]
    assert items[2]["file"] == "img_00002.png"


def test_upscale_tiles_and_zip_safety(tmp_path):
    ep = _load("upscale")
    boxes = ep.tile_boxes(100, 150, 64)
    covered = sum((y1 - y0) * (x1 - x0) for y0, y1, x0, x1 in boxes)
    assert covered == 100 * 150 and ep.tile_boxes(10, 10, 0) == [(0, 10, 0, 10)]
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("a.png", b"x")
    with zipfile.ZipFile(good) as z:
        assert [i.filename for i in ep.safe_zip_members(z)] == ["a.png"]
    for bad_name in ("../x.png", "/abs.png", "d/x.png", "x.exe"):
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr(bad_name, b"x")
        with zipfile.ZipFile(bad) as z, pytest.raises(kitrt.KitError):
            ep.safe_zip_members(z)


def test_commit_marker_roundtrip_and_tamper(tmp_path, monkeypatch):
    f = tmp_path / "frames" / "f1.png"
    f.parent.mkdir()
    f.write_bytes(b"frame-one")
    kitrt.commit([str(f)], 1, root=str(tmp_path))
    monkeypatch.setattr(kitrt, "RESUME", True)
    assert list(kitrt.committed_files(str(tmp_path))) == ["frames/f1.png"]
    f.write_bytes(b"tampered")  # a file changed after COMMIT is not trusted
    assert kitrt.committed_files(str(tmp_path)) == {}
    monkeypatch.setattr(kitrt, "RESUME", False)
    assert kitrt.committed_files(str(tmp_path)) == {}


def test_smoke_specs_list_three_gpu_models_and_pending_fixtures():
    for slug in SLUGS:
        s = json.loads((KITS / slug / "smoke" / "smoke.json").read_text())
        assert len(set(s["gpu_models_required"])) >= 3


def test_nan_and_infinity_params_are_refused_not_waved_through_the_bounds():
    """NaN > maximum and NaN < minimum are both False, so NaN used to pass a bounded number param."""
    schema = {"type": "object", "properties": {"g": {"type": "number", "minimum": 0, "maximum": 10}}}
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(kitrt.KitError):
            kitrt.validate({"g": bad}, schema)
    assert kitrt.validate({"g": 7.5}, schema) == {"g": 7.5}
