"""upscale Kit entrypoint: Real-ESRGAN (x4plus) via spandrel (container contract: kitrt).

Input `source` is one image (PNG/JPEG/WEBP) or a .zip of such images (flat names only, caps below).
Determinism: fp32, torch deterministic algorithms, fixed tiling. Different GPU models can still differ in the
last bits, so verification is perceptual. Checkpoint: one PNG per input image + COMMIT.
spandrel API to verify against current docs before use (L4): ModelLoader().load_from_file(path) returns an
ImageModelDescriptor, callable on a BCHW float tensor in [0, 1]; descriptor.scale; .to(device); .eval().
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
WEIGHT_FILES = {"realesrgan-x4plus": "RealESRGAN_x4plus.pth",
                "realesrgan-x4plus-anime": "RealESRGAN_x4plus_anime_6B.pth"}
MAX_IMAGES = 500
MAX_ZIP_BYTES = 2 * 1024**3
MAX_INPUT_PIXELS = 40_000_000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,100}\.(png|jpg|jpeg|webp)$", re.I)
PAD = 16


def safe_zip_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Flat, whitelisted names only; caps on count and size. Anything else refuses the whole archive. Pure."""
    members = [i for i in zf.infolist() if not i.is_dir()]
    if not members or len(members) > MAX_IMAGES:
        raise kitrt.KitError("invalid_input", "zip must hold 1..%d images" % MAX_IMAGES)
    if sum(i.file_size for i in members) > MAX_ZIP_BYTES:
        raise kitrt.KitError("invalid_input", "zip too large when unpacked")
    names = set()
    stems = set()
    for i in members:
        if not NAME_RE.match(i.filename) or "/" in i.filename or "\\" in i.filename or i.filename in names:
            raise kitrt.KitError("invalid_input", "unsafe or duplicate name in zip")
        names.add(i.filename)
        # The output is <stem>_x<scale>.png: "a.png" and "a.jpg" would write the same file (and the same checkpoint key),
        # silently dropping one image while reporting two results.
        stem = os.path.splitext(i.filename)[0].lower()
        if stem in stems:
            raise kitrt.KitError("invalid_input", "two images share the same name before the extension")
        stems.add(stem)
    return sorted(members, key=lambda i: i.filename)


def tile_boxes(h: int, w: int, tile: int) -> list[tuple[int, int, int, int]]:
    """Tile grid (y0, y1, x0, x1) covering the image; tile=0 means one tile. Pure."""
    if tile <= 0 or (h <= tile and w <= tile):
        return [(0, h, 0, w)]
    boxes = []
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            boxes.append((y, min(y + tile, h), x, min(x + tile, w)))
    return boxes


def upscale_tensor(model, x, tile: int):
    """x: 1x3xHxW float tensor on the model device. Tiles with PAD pixels of context, then crops the padding."""
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


def collect_inputs(source: str, work: str) -> list[tuple[str, str]]:
    """Returns [(output stem, path)] for a single image or a zip. Zip members are extracted by hand."""
    with open(source, "rb") as fh:
        magic = fh.read(4)
    if magic == b"PK\x03\x04":
        items = []
        with zipfile.ZipFile(source) as zf:
            for info in safe_zip_members(zf):
                dest = os.path.join(work, "in_" + info.filename)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                items.append((os.path.splitext(info.filename)[0], dest))
        return items
    return [("image", source)]


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    source = kitrt.input_file("source")
    out_dir = os.path.join(kitrt.OUT_DIR, "upscaled")
    work = os.path.join(kitrt.OUT_DIR, ".work")
    for d in (out_dir, work, kitrt.CKPT_DIR):
        os.makedirs(d, exist_ok=True)
    kitrt.verify_manifest(WEIGHTS, MANIFEST)
    items = collect_inputs(source, work)

    done = kitrt.committed_files()
    committed = list(done.values())
    seq = len(done)
    model = None
    results = []
    for stem, path in items:
        name = f"{stem}_x{params['scale']}.png"
        rel = f"upscaled/{name}"
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
                dev = "cuda" if torch.cuda.is_available() else "cpu"
                model = ModelLoader().load_from_file(os.path.join(WEIGHTS, WEIGHT_FILES[params["model"]]))
                model.to(dev).eval()
            Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS
            with Image.open(path, formats=["PNG", "JPEG", "WEBP"]) as im:
                if im.width * im.height > MAX_INPUT_PIXELS:
                    raise kitrt.KitError("invalid_input", f"{stem}: image too large")
                rgb = im.convert("RGB")
            arr = np.asarray(rgb, dtype=np.float32) / 255.0
            x = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(next(model.model.parameters()).device)
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
        results.append({"file": name, "sha256": kitrt.sha256_file(os.path.join(out_dir, name))})
    shutil.rmtree(work, ignore_errors=True)
    return {"kit": "upscale", "model": params["model"], "scale": params["scale"], "images": results,
            "resumed_images": len(done)}


if __name__ == "__main__":
    kitrt.run_main(main)
