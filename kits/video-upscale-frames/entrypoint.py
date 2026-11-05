"""video-upscale-frames Kit entrypoint: upscale an ordered zip of numbered frames with Real-ESRGAN via spandrel.

Pipeline helper (ffmpeg-transcode extracts frames, this Kit upscales, ffmpeg-transcode encodes). Per-frame fp32
inference, fixed tiling, so frame ranges can be fanned out. Checkpoint: one PNG per frame + COMMIT.
spandrel API to verify against current docs before use (L4): ModelLoader().load_from_file(path) returns an
ImageModelDescriptor, callable on a BCHW float tensor in [0, 1]; descriptor.scale; .to(device); .eval().
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
WEIGHT_FILES = {"realesrgan-x4plus": "RealESRGAN_x4plus.pth", "realesrgan-x4plus-anime": "RealESRGAN_x4plus_anime_6B.pth"}
MAX_FRAMES = 20_000
MAX_ZIP_BYTES = 8 * 1024**3
MAX_INPUT_PIXELS = 16_000_000
NAME_RE = re.compile(r"^([0-9]{1,9})\.(png|jpg|jpeg)$", re.I)
PAD = 16


def frame_members(zf: zipfile.ZipFile) -> list[tuple[int, zipfile.ZipInfo]]:
    """Flat numeric names only, contiguous numbering, caps on count and size, sorted by number. Anything else refuses
    the archive. Pure."""
    members = [i for i in zf.infolist() if not i.is_dir()]
    if not members or len(members) > MAX_FRAMES:
        raise kitrt.KitError("invalid_input", "zip must hold 1..%d frames" % MAX_FRAMES)
    if sum(i.file_size for i in members) > MAX_ZIP_BYTES:
        raise kitrt.KitError("invalid_input", "zip too large when unpacked")
    numbered: dict[int, zipfile.ZipInfo] = {}
    for i in members:
        m = NAME_RE.match(i.filename)
        if not m or "/" in i.filename or "\\" in i.filename:
            raise kitrt.KitError("invalid_input", "unsafe or non-numeric frame name")
        n = int(m.group(1))
        if n in numbered:
            raise kitrt.KitError("invalid_input", "duplicate frame number")
        numbered[n] = i
    nums = sorted(numbered)
    if nums[-1] - nums[0] + 1 != len(nums):
        raise kitrt.KitError("invalid_input", "frame numbers must be contiguous")
    return [(n, numbered[n]) for n in nums]


def tile_boxes(h: int, w: int, tile: int) -> list[tuple[int, int, int, int]]:
    if tile <= 0 or (h <= tile and w <= tile):
        return [(0, h, 0, w)]
    return [(y, min(y + tile, h), x, min(x + tile, w)) for y in range(0, h, tile) for x in range(0, w, tile)]


def upscale_tensor(model, x, tile: int):
    import torch

    _, _, h, w = x.shape
    s = int(model.scale)
    out = torch.zeros((1, 3, h * s, w * s), dtype=torch.float32, device=x.device)
    with torch.inference_mode():
        for (y0, y1, x0, x1) in tile_boxes(h, w, tile):
            py0, py1, px0, px1 = max(0, y0 - PAD), min(h, y1 + PAD), max(0, x0 - PAD), min(w, x1 + PAD)
            y_t = model(x[:, :, py0:py1, px0:px1]).clamp_(0, 1)
            oy0, ox0 = (y0 - py0) * s, (x0 - px0) * s
            out[:, :, y0 * s:y1 * s, x0 * s:x1 * s] = y_t[:, :, oy0:oy0 + (y1 - y0) * s, ox0:ox0 + (x1 - x0) * s]
    return out


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    source = kitrt.input_file("frames")
    out_dir = os.path.join(kitrt.OUT_DIR, "frames")
    work = os.path.join(kitrt.OUT_DIR, ".work")
    for d in (out_dir, work, kitrt.CKPT_DIR):
        os.makedirs(d, exist_ok=True)
    kitrt.verify_manifest(WEIGHTS, MANIFEST)
    with open(source, "rb") as fh:
        if fh.read(4) != b"PK\x03\x04":
            raise kitrt.KitError("invalid_input", "frames must be a zip")
    done = kitrt.committed_files()
    committed = list(done.values())
    seq = len(done)
    model = None
    count = 0
    with zipfile.ZipFile(source) as zf:
        members = frame_members(zf)
        for index, (num, info) in enumerate(members, 1):
            name = f"frame_{index:07d}.png"
            rel = f"frames/{name}"
            ck = os.path.join(kitrt.CKPT_DIR, rel)
            if rel in done:
                shutil.copyfile(done[rel], os.path.join(out_dir, name))
            else:
                import numpy as np
                import torch
                from PIL import Image

                if model is None:
                    from spandrel import ModelLoader

                    torch.use_deterministic_algorithms(True, warn_only=True)
                    torch.backends.cudnn.deterministic = True
                    torch.backends.cudnn.benchmark = False
                    if not torch.cuda.is_available():
                        raise kitrt.KitError("gpu_unavailable", "no CUDA device visible")
                    model = ModelLoader().load_from_file(os.path.join(WEIGHTS, WEIGHT_FILES[params["model"]]))
                    model.to("cuda").eval()
                src = os.path.join(work, f"in_{num}.img")
                with zf.open(info) as s, open(src, "wb") as d:
                    shutil.copyfileobj(s, d, 1 << 20)
                Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS
                with Image.open(src, formats=["PNG", "JPEG"]) as im:
                    if im.width * im.height > MAX_INPUT_PIXELS:
                        raise kitrt.KitError("invalid_input", f"frame {num}: image too large")
                    rgb = im.convert("RGB")
                os.remove(src)
                x = torch.from_numpy(np.asarray(rgb, dtype=np.float32) / 255.0).permute(2, 0, 1).unsqueeze(0).to("cuda")
                y = upscale_tensor(model, x, params["tile"])
                res = Image.fromarray((y[0].permute(1, 2, 0).cpu().numpy() * 255.0 + 0.5).astype("uint8"), "RGB")
                if params["scale"] != int(model.scale):
                    res = res.resize((rgb.width * params["scale"], rgb.height * params["scale"]), Image.LANCZOS)
                os.makedirs(os.path.dirname(ck), exist_ok=True)
                res.save(ck + ".part", format="PNG")
                os.replace(ck + ".part", ck)
                committed.append(ck)
                seq += 1
                kitrt.commit(committed, seq)
                shutil.copyfile(ck, os.path.join(out_dir, name))
            count += 1
    shutil.rmtree(work, ignore_errors=True)
    manifest = {"frame_count": count, "scale": params["scale"], "model": params["model"], "fps": params["fps"],
                "pattern": "frame_%07d.png"}
    with open(os.path.join(kitrt.OUT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, sort_keys=True)
    return {"kit": "video-upscale-frames", **manifest, "resumed_frames": len(done)}


if __name__ == "__main__":
    kitrt.run_main(main)
