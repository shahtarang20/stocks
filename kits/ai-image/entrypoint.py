"""ai-image Kit entrypoint: text-to-image with FLUX.1-schnell via diffusers (container contract: see kitrt).

Determinism: image i of a job uses seed `seed + i` on a CPU torch.Generator (CPU noise is identical on
every GPU model), fixed steps, guidance 0 (schnell), fp bf16. Different GPU models still differ slightly
in float results, so verification is perceptual, not byte-exact.
Checkpoints: after each image the PNG is written to the checkpoint dir and COMMIT is replaced.
Library calls to verify against current docs before use (L4): diffusers FluxPipeline.from_pretrained,
enable_model_cpu_offload, pipeline call arguments (num_inference_steps, guidance_scale, max_sequence_length, generator).
"""

from __future__ import annotations

import os
import shutil

import kitrt

MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/model")
MANIFEST = os.environ.get("MODEL_MANIFEST", "/opt/model.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
MAX_SEQ = 256  # schnell's T5 limit


def plan(params: dict) -> list[dict]:
    """Per-image plan; pure, so it can be tested without a GPU."""
    items = []
    for i, prompt in enumerate(params["prompts"]):
        items.append({"index": i, "prompt": prompt, "seed": (params["seed"] + i) % (2**32),
                      "file": f"img_{i:05d}.png"})
    return items


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    items = plan(params)
    out_dir = os.path.join(kitrt.OUT_DIR, "images")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(kitrt.CKPT_DIR, exist_ok=True)
    manifest_digest = kitrt.verify_manifest(MODEL_DIR, MANIFEST)

    done = kitrt.committed_files()
    todo = [it for it in items if f"images/{it['file']}" not in done]
    pipe = None
    if todo:
        import torch
        from diffusers import FluxPipeline

        torch.use_deterministic_algorithms(True, warn_only=True)
        pipe = FluxPipeline.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16, local_files_only=True)
        pipe.enable_model_cpu_offload()
    seq = len(done)
    committed: list[str] = [p for p in done.values()]
    for it in items:
        rel = f"images/{it['file']}"
        ck = os.path.join(kitrt.CKPT_DIR, rel)
        if rel in done:
            shutil.copyfile(done[rel], os.path.join(out_dir, it["file"]))
            continue
        gen = torch.Generator(device="cpu").manual_seed(it["seed"])
        image = pipe(prompt=it["prompt"], width=params["width"], height=params["height"],
                     num_inference_steps=params["steps"], guidance_scale=0.0,
                     max_sequence_length=MAX_SEQ, generator=gen).images[0]
        os.makedirs(os.path.dirname(ck), exist_ok=True)
        image.save(ck + ".part", format="PNG")
        os.replace(ck + ".part", ck)
        committed.append(ck)
        seq += 1
        kitrt.commit(committed, seq)
        shutil.copyfile(ck, os.path.join(out_dir, it["file"]))

    results = [{"index": it["index"], "seed": it["seed"], "file": it["file"],
                "sha256": kitrt.sha256_file(os.path.join(out_dir, it["file"]))} for it in items]
    return {"kit": "ai-image", "model_manifest_sha256": manifest_digest, "images": results,
            "resumed_images": len(done)}


if __name__ == "__main__":
    kitrt.run_main(main)
