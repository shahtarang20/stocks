"""P3: workload entrypoint contract (no GPU: tiny fake backend) and image registration."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3] / "workloads"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"wl_{name}", ROOT / name / "entrypoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _model_dir(tmp_path, mod):
    d = tmp_path / "model"
    d.mkdir()
    (d / "w.bin").write_bytes(b"weights")
    man = tmp_path / "model.sha256"
    man.write_text(f"{hashlib.sha256(b'weights').hexdigest()}  w.bin\n")
    mod.MODEL_DIR, mod.MANIFEST = str(d), str(man)
    return d, man


def test_text_generation_contract(tmp_path):
    mod = _load("text-generation")
    _model_dir(tmp_path, mod)
    (tmp_path / "input.json").write_text(json.dumps({"prompt": "hi", "max_tokens": 5}))
    env = {
        "COMPUTECOIN_INPUT": str(tmp_path / "input.json"),
        "COMPUTECOIN_OUTPUT": str(tmp_path / "output.json"),
        "COMPUTECOIN_WORKLOAD_TYPE": "text-generation",
        "COMPUTECOIN_MODEL": "tinyllama-1.1b-chat-v1",
    }
    backend = lambda msgs, n: ("hello " + msgs[-1]["content"], 3, 2)
    a = mod.run(env, backend=backend)
    first = (tmp_path / "output.json").read_bytes()
    mod.run(env, backend=backend)
    assert first == (tmp_path / "output.json").read_bytes()  # byte-identical
    assert isinstance(a["text"], str) and a["tokens"] == 2 and a["simulated"] is False


def test_manifest_mismatch_fails(tmp_path):
    mod = _load("text-generation")
    d, _ = _model_dir(tmp_path, mod)
    (d / "w.bin").write_bytes(b"tampered")
    with pytest.raises(mod.WorkloadError):
        mod.verify_manifest(str(d), mod.MANIFEST)


def test_embeddings_contract(tmp_path):
    mod = _load("embeddings")
    _model_dir(tmp_path, mod)
    (tmp_path / "input.json").write_text(json.dumps({"input": ["a", "b"]}))
    env = {
        "COMPUTECOIN_INPUT": str(tmp_path / "input.json"),
        "COMPUTECOIN_OUTPUT": str(tmp_path / "output.json"),
        "COMPUTECOIN_WORKLOAD_TYPE": "embeddings",
    }
    out = mod.run(env, backend=lambda t: ([[1.0, 0.0] for _ in t], 2))
    assert len(out["embeddings"]) == 2 and out["dim"] == 2


def test_dockerfiles_pin_and_run_non_root():
    for wl in ("text-generation", "embeddings"):
        for rt in ("cuda", "rocm"):
            text = (ROOT / wl / f"Dockerfile.{rt}").read_text()
            assert "ARG BASE_IMAGE" in text and "@sha256:" in text
            assert "USER 10001" in text and "HF_HUB_OFFLINE=1" in text
            assert "EXPECTED_MANIFEST_SHA256" in text


@pytest.mark.asyncio
async def test_register_image_logs_image_hash(client):
    from app.db.session import get_sessionmaker
    from app.models.model_registry import Model, ModelVersion
    from app.models.privacy import TransparencyLogLeaf
    from app.services.workload_images import ImageRegistrationError, register_image
    from sqlalchemy import select

    async with get_sessionmaker()() as s:
        m = Model(name="tl-reg", trust_status="approved")
        s.add(m)
        await s.flush()
        s.add(ModelVersion(model_id=m.id, version_tag="tl-reg-v1", runtime="hardware",
                           supported_workloads=["text-generation"], trust_status="approved"))
        await s.commit()
        digest = "ghcr.io/x/img@sha256:" + "a" * 64
        row, idx = await register_image(s, version_tag="tl-reg-v1", gpu_runtime="rocm", image_digest=digest)
        assert row.image_digest == digest
        leaf = (await s.execute(select(TransparencyLogLeaf).where(TransparencyLogLeaf.leaf_index == idx))).scalar_one()
        assert leaf.event_type == "IMAGE_HASH" and leaf.payload["gpu_runtime"] == "rocm"
        with pytest.raises(ImageRegistrationError):
            await register_image(s, version_tag="tl-reg-v1", gpu_runtime="cuda", image_digest="img:latest")
