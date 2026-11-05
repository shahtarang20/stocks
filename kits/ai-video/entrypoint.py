"""ai-video Kit entrypoint: CogVideoX text/image-to-video via diffusers, frames encoded to mp4 with ffmpeg
(container contract: see kitrt).

Determinism: video i of a job uses seed `seed + i` on a CPU torch.Generator (CPU noise is identical on every GPU model),
fixed steps and guidance, bf16. Different GPU models still differ in float results (more than a re-encode does), so
verification is psnr_ssim with deliberately loose OWNER DECISION thresholds, not byte-exact. The encode itself is made
reproducible (single thread, bitexact flags, no metadata, constant frame rate).
Checkpoints: after each video the mp4 is written to the checkpoint dir and COMMIT is replaced.
Library calls to verify against current docs before use (L4): diffusers CogVideoXPipeline / CogVideoXImageToVideoPipeline
.from_pretrained(local_files_only=) and their __call__ arguments (prompt, image, height, width, num_frames,
num_inference_steps, guidance_scale, generator), `.frames[0]` as a list of PIL images; imageio_ffmpeg.get_ffmpeg_exe();
ffmpeg flags: -f rawvideo -pix_fmt rgb24 -s -r -i pipe:0, -c:v libx264 -threads 1 -bitexact -fflags +bitexact.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import kitrt

MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/model")
MANIFEST = os.environ.get("MODEL_MANIFEST", "/opt/model.sha256")
KIND_FILE = os.environ.get("MODEL_KIND_FILE", "/opt/model.kind")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
MAX_IMAGE_BYTES = 32 * 1024**2
MAX_IMAGE_PIXELS = 50_000_000
ENCODE_TIMEOUT_S = 600


def plan(params: dict) -> list[dict]:
    """Per-video plan; pure, so it can be tested without a GPU."""
    return [{"index": i, "prompt": p, "seed": (params["seed"] + i) % (2**32), "file": f"video_{i:05d}.mp4"}
            for i, p in enumerate(params["prompts"])]


def ffmpeg_cmd(ffmpeg: str, width: int, height: int, fps: int, crf: int, out_path: str) -> list[str]:
    """Reproducible libx264 encode of raw RGB frames on stdin. argv list, never a shell. Pure."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-protocol_whitelist", "pipe,file",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p", "-threads", "1",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1", "-movflags", "+faststart",
            "-f", "mp4", out_path]


def encode(frames, width: int, height: int, fps: int, crf: int, out_path: str) -> None:
    """Pipe PIL frames (RGB) into ffmpeg."""
    import imageio_ffmpeg

    proc = subprocess.Popen(ffmpeg_cmd(imageio_ffmpeg.get_ffmpeg_exe(), width, height, fps, crf, out_path),
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdin is not None
        for fr in frames:
            fr = fr.convert("RGB")
            if fr.size != (width, height):
                raise kitrt.KitError("internal_error", "unexpected frame size")
            proc.stdin.write(fr.tobytes())
        proc.stdin.close()
        _, err = proc.communicate(timeout=ENCODE_TIMEOUT_S)
    except Exception:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise kitrt.KitError("encode_failed", err.decode(errors="replace")[-300:])


def load_conditioning_image(path: str):
    from PIL import Image

    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        raise kitrt.KitError("invalid_input", "image too large")
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(path, formats=["PNG", "JPEG", "WEBP"]) as im:
            im.load()
            return im.convert("RGB")
    except Exception as exc:
        raise kitrt.KitError("invalid_input", f"unreadable image: {type(exc).__name__}") from exc


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    items = plan(params)
    kind = open(KIND_FILE, encoding="utf-8").read().strip() if os.path.isfile(KIND_FILE) else "t2v"
    image_path = kitrt.input_file("image", required=False)
    if kind == "i2v" and image_path is None:
        raise kitrt.KitError("missing_input", "image (this image bakes an image-to-video model)")
    if kind == "t2v" and image_path is not None:
        raise kitrt.KitError("invalid_input", "this image bakes a text-to-video model; do not supply an image")
    out_dir = os.path.join(kitrt.OUT_DIR, "videos")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(kitrt.CKPT_DIR, exist_ok=True)
    manifest_digest = kitrt.verify_manifest(MODEL_DIR, MANIFEST)

    done = kitrt.committed_files()
    todo = [it for it in items if f"videos/{it['file']}" not in done]
    pipe = None
    cond = None
    if todo:
        import torch
        from diffusers import CogVideoXImageToVideoPipeline, CogVideoXPipeline

        torch.use_deterministic_algorithms(True, warn_only=True)
        cls = CogVideoXImageToVideoPipeline if kind == "i2v" else CogVideoXPipeline
        pipe = cls.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16, local_files_only=True)
        pipe.enable_model_cpu_offload()
        pipe.vae.enable_tiling()
        if kind == "i2v":
            cond = load_conditioning_image(image_path)
    seq = len(done)
    committed: list[str] = list(done.values())
    for it in items:
        rel = f"videos/{it['file']}"
        ck = os.path.join(kitrt.CKPT_DIR, rel)
        if rel in done:
            shutil.copyfile(done[rel], os.path.join(out_dir, it["file"]))
            continue
        gen = torch.Generator(device="cpu").manual_seed(it["seed"])
        kwargs = dict(prompt=it["prompt"], height=params["height"], width=params["width"],
                      num_frames=params["num_frames"], num_inference_steps=params["steps"],
                      guidance_scale=params["guidance_scale"], generator=gen)
        if cond is not None:
            kwargs["image"] = cond
        frames = pipe(**kwargs).frames[0]
        os.makedirs(os.path.dirname(ck), exist_ok=True)
        encode(frames, params["width"], params["height"], params["fps"], params["crf"], ck + ".part")
        os.replace(ck + ".part", ck)
        committed.append(ck)
        seq += 1
        kitrt.commit(committed, seq)
        shutil.copyfile(ck, os.path.join(out_dir, it["file"]))

    results = [{"index": it["index"], "seed": it["seed"], "file": it["file"],
                "sha256": kitrt.sha256_file(os.path.join(out_dir, it["file"]))} for it in items]
    return {"kit": "ai-video", "model_kind": kind, "model_manifest_sha256": manifest_digest, "videos": results,
            "resumed_videos": len(done)}


if __name__ == "__main__":
    kitrt.run_main(main)
