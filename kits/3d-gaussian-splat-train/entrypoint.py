"""3d-gaussian-splat-train Kit entrypoint: optional COLMAP, then gsplat's example trainer (container contract: kitrt).

Uses gsplat (Apache-2.0), NOT the non-commercial graphdeco-inria code. All subprocesses run with fixed argument lists, no
shell. The dataset zip is extracted by hand with a whitelist (README). No resume in v1.
VERIFY against the pinned releases (L4): colmap CLI (feature_extractor, exhaustive_matcher / sequential_matcher, mapper)
options, and gsplat examples/simple_trainer.py arguments (--data_dir, --result_dir, --max_steps, --data_factor, subcommand).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
TRAINER = os.path.join(WEIGHTS, "gsplat", "examples", "simple_trainer.py")
MAX_IMAGES = 500
MAX_MEMBERS = 600
MAX_ZIP_BYTES = 8 * 1024**3
MAX_PIXELS = 40_000_000
IMG_RE = re.compile(r"^images/[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.(png|jpg|jpeg)$", re.I)
SPARSE_RE = re.compile(r"^sparse/0/(cameras|images|points3D)\.(bin|txt)$")


def check_members(zf: zipfile.ZipFile, need_sparse: bool) -> list[zipfile.ZipInfo]:
    """Whitelist of exactly images/<flat name> and sparse/0/<colmap files>. Pure."""
    members = [i for i in zf.infolist() if not i.is_dir()]
    if not members or len(members) > MAX_MEMBERS:
        raise kitrt.KitError("invalid_input", "unexpected member count")
    if sum(i.file_size for i in members) > MAX_ZIP_BYTES:
        raise kitrt.KitError("invalid_input", "zip too large when unpacked")
    seen = set()
    images = 0
    sparse = 0
    for i in members:
        n = i.filename
        if n in seen or "\\" in n or n.startswith("/") or ".." in n.split("/"):
            raise kitrt.KitError("invalid_input", "unsafe or duplicate name in zip")
        seen.add(n)
        if IMG_RE.match(n):
            images += 1
        elif SPARSE_RE.match(n):
            sparse += 1
        else:
            raise kitrt.KitError("invalid_input", "unlisted file in zip")
    if images < 3 or images > MAX_IMAGES:
        raise kitrt.KitError("invalid_input", "need 3..%d images" % MAX_IMAGES)
    if need_sparse and sparse < 3:
        raise kitrt.KitError("invalid_input", "run_colmap=false needs sparse/0 with cameras, images and points3D")
    return members


def colmap_cmds(db: str, images: str, sparse: str, matcher: str) -> list[list[str]]:
    """Fixed COLMAP command lines (no shell). Pure."""
    return [
        ["colmap", "feature_extractor", "--database_path", db, "--image_path", images, "--ImageReader.single_camera", "1"],
        ["colmap", f"{matcher}_matcher", "--database_path", db],
        ["colmap", "mapper", "--database_path", db, "--image_path", images, "--output_path", sparse],
    ]


def trainer_cmd(data_dir: str, result_dir: str, iterations: int, sh_degree: int) -> list[str]:
    return [sys.executable, TRAINER, "default", "--data_dir", data_dir, "--result_dir", result_dir,
            "--max_steps", str(iterations), "--sh_degree", str(sh_degree), "--disable_viewer"]


def run(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env={**os.environ, "HOME": "/tmp"})
    if r.returncode != 0:
        raise kitrt.KitError(what + "_failed", r.stderr.decode(errors="replace")[-300:])


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    source = kitrt.input_file("dataset")
    kitrt.verify_manifest(WEIGHTS, MANIFEST)
    work = os.path.join(kitrt.OUT_DIR, ".work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    with zipfile.ZipFile(source) as zf:
        members = check_members(zf, need_sparse=not params["run_colmap"])
        for info in members:
            dest = os.path.join(work, info.filename)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as s, open(dest, "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    for name in sorted(os.listdir(os.path.join(work, "images"))):
        with Image.open(os.path.join(work, "images", name), formats=["PNG", "JPEG"]) as im:
            if im.width * im.height > MAX_PIXELS:
                raise kitrt.KitError("invalid_input", f"{name}: image too large")
    if params["run_colmap"]:
        sparse = os.path.join(work, "sparse")
        os.makedirs(sparse, exist_ok=True)
        for cmd in colmap_cmds(os.path.join(work, "colmap.db"), os.path.join(work, "images"), sparse, params["colmap_matcher"]):
            run(cmd, "colmap")
        if not os.path.isdir(os.path.join(sparse, "0")):
            raise kitrt.KitError("colmap_failed", "no sparse model was reconstructed (too little overlap?)")
    result = os.path.join(work, "result")
    run(trainer_cmd(work, result, params["iterations"], params["sh_degree"]), "training")
    model_dir = os.path.join(kitrt.OUT_DIR, "model")
    preview_dir = os.path.join(kitrt.OUT_DIR, "preview")
    os.makedirs(model_dir, exist_ok=True)
    os.makedirs(preview_dir, exist_ok=True)
    files, previews = [], []
    for root, _dirs, names in os.walk(result):
        for n in sorted(names):
            p = os.path.join(root, n)
            if os.path.islink(p):
                continue
            rel = os.path.relpath(p, result).replace(os.sep, "__")
            if n.lower().endswith(".png"):
                shutil.copyfile(p, os.path.join(preview_dir, rel))
                previews.append(rel)
            elif n.lower().endswith((".ply", ".pt", ".json")):
                shutil.copyfile(p, os.path.join(model_dir, rel))
                files.append(rel)
    shutil.rmtree(work, ignore_errors=True)
    if not files or not previews:
        raise kitrt.KitError("no_outputs", "trainer produced no model or no validation renders")
    return {"kit": "3d-gaussian-splat-train", "iterations": params["iterations"], "model_files": sorted(files),
            "preview_files": sorted(previews)}


if __name__ == "__main__":
    kitrt.run_main(main)
