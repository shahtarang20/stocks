"""blender-render Kit entrypoint: render a frame range with the Blender command line (container contract: kitrt).

One Blender process per frame so each finished frame can be checkpointed (COMMIT marker) and a resumed job
skips frames that are already committed. Auto-run scripts in the customer's .blend are disabled.
Blender CLI flags to verify against current docs before use (L4): -b, --disable-autoexec, --python, -o, -F, -x, -f.
Reference: https://docs.blender.org/manual/en/latest/advanced/command_line/arguments.html (not yet checked by this work).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import kitrt

BLENDER = os.environ.get("BLENDER_BIN", "/opt/blender/blender")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
SETUP = os.environ.get("BLENDER_SETUP", "/app/blender_setup.py")
MAX_FRAMES = 20000
FRAME_TIMEOUT_S = int(os.environ.get("FRAME_TIMEOUT_S", "14400"))


def frame_list(params: dict) -> list[int]:
    """Frames to render, inclusive range with step. Pure."""
    a, b, step = params["frame_start"], params["frame_end"], params["frame_step"]
    if b < a:
        raise kitrt.KitError("invalid_param", "frame_end must be >= frame_start")
    frames = list(range(a, b + 1, step))
    if len(frames) > MAX_FRAMES:
        raise kitrt.KitError("invalid_param", f"more than {MAX_FRAMES} frames")
    return frames


def frame_name(n: int) -> str:
    return f"frame_{n:07d}.png"


def build_cmd(blend: str, frame: int, out_prefix: str, setup_json: str) -> list[str]:
    """Argv for one frame. Pure. Order matters: the setup script runs after the file loads and before rendering."""
    return [BLENDER, "-b", blend, "--disable-autoexec", "--python", SETUP, "-o", out_prefix, "-F", "PNG",
            "-x", "1", "-f", str(frame), "--", setup_json]


def render_frame(blend: str, frame: int, params: dict, work: str) -> str:
    setup_json = os.path.join(work, "setup.json")
    with open(setup_json, "w", encoding="utf-8") as fh:
        json.dump({"engine": params["engine"], "device": params["device"], "samples": params["samples"],
                   "resolution_percent": params["resolution_percent"], "seed": params["seed"],
                   "denoise": params["denoise"]}, fh)
    prefix = os.path.join(work, "render_####")
    proc = subprocess.run(build_cmd(blend, frame, prefix, setup_json), capture_output=True, timeout=FRAME_TIMEOUT_S,
                          check=False, shell=False)
    produced = os.path.join(work, f"render_{frame:04d}.png")
    # Frames above 9999 get more digits from Blender; find the one file this call made.
    if not os.path.isfile(produced):
        cands = [f for f in os.listdir(work) if f.startswith("render_") and f.endswith(".png")]
        produced = os.path.join(work, cands[0]) if len(cands) == 1 else produced
    if proc.returncode != 0 or not os.path.isfile(produced):
        tail = (proc.stdout + proc.stderr)[-600:].decode("utf-8", "replace")
        if "DEVICE_UNAVAILABLE" in tail:
            raise kitrt.KitError("device_unavailable", tail)
        raise kitrt.KitError("render_failed", f"frame {frame}: exit {proc.returncode}: {tail}")
    return produced


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    frames = frame_list(params)
    scene = kitrt.input_file("scene")
    out_dir = os.path.join(kitrt.OUT_DIR, "frames")
    work = os.path.join(kitrt.OUT_DIR, ".work")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(kitrt.CKPT_DIR, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    # Blender wants a .blend name; the staged input has none. A symlink in scratch space is read-only access.
    blend = os.path.join(work, "scene.blend")
    if os.path.lexists(blend):
        os.remove(blend)
    os.symlink(scene, blend)

    done = kitrt.committed_files()
    committed = list(done.values())
    seq = len(done)
    results = []
    for n in frames:
        name = frame_name(n)
        rel = f"frames/{name}"
        if rel in done:
            shutil.copyfile(done[rel], os.path.join(out_dir, name))
        else:
            produced = render_frame(blend, n, params, work)
            ck = os.path.join(kitrt.CKPT_DIR, rel)
            os.makedirs(os.path.dirname(ck), exist_ok=True)
            shutil.move(produced, ck + ".part")
            os.replace(ck + ".part", ck)
            committed.append(ck)
            seq += 1
            kitrt.commit(committed, seq)
            shutil.copyfile(ck, os.path.join(out_dir, name))
        results.append({"frame": n, "file": name, "sha256": kitrt.sha256_file(os.path.join(out_dir, name))})
    shutil.rmtree(work, ignore_errors=True)
    return {"kit": "blender-render", "engine": params["engine"], "device": params["device"], "frames": results,
            "resumed_frames": len(done)}


if __name__ == "__main__":
    kitrt.run_main(main)
