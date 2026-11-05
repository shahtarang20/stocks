"""Wave 2/3 Kit checks (WRITTEN, NOT RUN). Run from repo root: pytest kits/tests. Pure Python only (no torch, GPU, docker).
Bug re-insertion list: drop the contiguity check in frame_members (gap test fails); drop the class_type allowlist test in
validate_workflow (allowlist test fails); remove the divisibility check in plan_accumulation (accumulation test fails);
allow '..' in check_members (zip test fails).
"""

from __future__ import annotations

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

import kit_ci  # noqa: E402
import kitrt  # noqa: E402

SLUGS = ["whisper-transcribe", "llm-finetune-lora", "video-upscale-frames", "comfyui-workflow-runner", "batch-embeddings",
         "3d-gaussian-splat-train"]


def _load(slug):
    spec = importlib.util.spec_from_file_location("ep23_" + slug.replace("-", "_"), KITS / slug / "entrypoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("slug", SLUGS)
def test_offline_gate_and_schema_defaults(slug):
    assert kit_ci.check_kit(KITS / slug) == []
    schema = json.loads((KITS / slug / "params.schema.json").read_text())
    out = kitrt.validate({}, schema)
    for k, sub in schema["properties"].items():
        assert out[k] == sub["default"]
    with pytest.raises(kitrt.KitError):
        kitrt.validate({"bogus": 1}, schema)


def test_srt_vtt_and_timestamps():
    ep = _load("whisper-transcribe")
    assert ep.fmt_ts(3661.5, ",") == "01:01:01,500"
    segs = [{"start": 0, "end": 1.25, "text": " hi "}]
    assert ep.to_srt(segs) == "1\n00:00:00,000 --> 00:00:01,250\nhi\n\n"
    assert ep.to_vtt(segs).startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.250")
    assert json.loads(ep.render("json", segs, "en", 1.25))["segments"] == segs


def test_accumulation_and_torchrun():
    ep = _load("llm-finetune-lora")
    assert ep.plan_accumulation(16, 2, 4) == 2
    with pytest.raises(kitrt.KitError):
        ep.plan_accumulation(10, 2, 4)
    cmd = ep.torchrun_cmd({"COMPUTECOIN_GPU_COUNT": "4"}, "/app/entrypoint.py")
    assert "--nproc_per_node=4" in cmd and "--standalone" in cmd
    multi = ep.torchrun_cmd({"COMPUTECOIN_GPU_COUNT": "4", "NNODES": "2", "MASTER_ADDR": "10.0.0.1", "MASTER_PORT": "29500",
                             "NODE_RANK": "1"}, "x.py")
    assert "--nnodes=2" in multi and "--node_rank=1" in multi and "--standalone" not in multi
    with pytest.raises(kitrt.KitError):
        ep.torchrun_cmd({"COMPUTECOIN_GPU_COUNT": "4", "NNODES": "2"}, "x.py")
    with pytest.raises(kitrt.KitError):
        ep.torchrun_cmd({"COMPUTECOIN_GPU_COUNT": "99"}, "x.py")


def test_dataset_loading(tmp_path):
    ep = _load("llm-finetune-lora")
    f = tmp_path / "d.jsonl"
    f.write_text('{"text": "a"}\n{"prompt": "p", "completion": "c"}\n')
    assert ep.load_examples(str(f)) == ["a", "pc"]
    f.write_text('{"nope": 1}\n')
    with pytest.raises(kitrt.KitError):
        ep.load_examples(str(f))


def test_frame_members_order_and_gaps(tmp_path):
    ep = _load("video-upscale-frames")

    def mk(names):
        p = tmp_path / "z.zip"
        with zipfile.ZipFile(p, "w") as z:
            for n in names:
                z.writestr(n, b"x")
        return zipfile.ZipFile(p)

    with mk(["0002.png", "0001.png"]) as z:
        assert [n for n, _ in ep.frame_members(z)] == [1, 2]
    for bad in (["1.png", "3.png"], ["a.png"], ["d/1.png"], ["../1.png"]):
        with mk(bad) as z, pytest.raises(kitrt.KitError):
            ep.frame_members(z)


def test_comfy_workflow_validation():
    ep = _load("comfyui-workflow-runner")
    ok = {"1": {"class_type": "KSampler", "inputs": {"seed": 1, "model": ["2", 0]}}}
    assert ep.validate_workflow(ok, {"KSampler"}) == ok
    with pytest.raises(kitrt.KitError):
        ep.validate_workflow(ok, {"Other"})
    for bad in ("../x.safetensors", "/etc/passwd", "http://x/y", "a\\b"):
        with pytest.raises(kitrt.KitError):
            ep.validate_workflow({"1": {"class_type": "KSampler", "inputs": {"ckpt": bad}}}, {"KSampler"})
    with pytest.raises(kitrt.KitError):
        ep.validate_workflow({}, {"KSampler"})


def test_embeddings_parse(tmp_path):
    ep = _load("batch-embeddings")
    f = tmp_path / "t.jsonl"
    f.write_text('{"id": "a", "text": "x"}\nplain\n')
    assert ep.parse_texts(str(f)) == (["x", "plain"], ["a", 1])
    f.write_text('{"id": "a"}\n')
    with pytest.raises(kitrt.KitError):
        ep.parse_texts(str(f))


def test_splat_zip_whitelist_and_colmap(tmp_path):
    ep = _load("3d-gaussian-splat-train")

    def mk(names):
        p = tmp_path / "s.zip"
        with zipfile.ZipFile(p, "w") as z:
            for n in names:
                z.writestr(n, b"x")
        return zipfile.ZipFile(p)

    good = [f"images/i{n}.jpg" for n in range(3)]
    with mk(good) as z:
        assert len(ep.check_members(z, need_sparse=False)) == 3
    with mk(good) as z, pytest.raises(kitrt.KitError):
        ep.check_members(z, need_sparse=True)
    for bad in (good + ["../x.jpg"], good + ["images/sub/x.jpg"], good + ["run.sh"], good + ["/abs.jpg"]):
        with mk(bad) as z, pytest.raises(kitrt.KitError):
            ep.check_members(z, need_sparse=False)
    cmds = ep.colmap_cmds("d.db", "imgs", "sp", "sequential")
    assert cmds[1][1] == "sequential_matcher" and all(isinstance(c, list) for c in cmds)


@pytest.mark.parametrize("slug,method", [("whisper-transcribe", "wer"), ("batch-embeddings", "cosine"),
                                          ("llm-finetune-lora", "loss_curve")])
def test_metric_verification_methods_replace_exact_placeholder(slug, method):
    v = json.loads((KITS / slug / "kit.json").read_text())["verification"]
    assert v["method"] == method
    assert "OWNER DECISION" in v["owner_decision"]
    assert v["thresholds"]  # explicit conservative value, validated by the plug-in's bounds in the API tests
