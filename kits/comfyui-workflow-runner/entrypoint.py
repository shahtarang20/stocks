"""comfyui-workflow-runner Kit entrypoint: run one API-format ComfyUI workflow headless (container contract: kitrt).

The workflow is validated BEFORE the server starts: size, node count, every class_type in the baked allowlist, and no
path-like string inputs. The server binds 127.0.0.1 only (the container has no network anyway). ComfyUI is GPL-3.0 and
runs as a separate, unmodified process (see README).
ComfyUI HTTP API to verify against the pinned commit (L4): POST /prompt {"prompt": workflow} -> {"prompt_id"};
GET /history/<prompt_id> -> {id: {"outputs": {node: {"images": [{"filename","subfolder","type"}]}}, "status": {...}}};
CLI flags used: --listen 127.0.0.1 --port --output-directory --disable-auto-launch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
COMFY_DIR = os.environ.get("COMFY_DIR", "/opt/weights/comfyui")
ALLOWED_NODES = os.environ.get("ALLOWED_NODES", "/opt/weights/allowed_nodes.json")
PORT = 8188
MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
MAX_NODES = 500
OUT_EXT = (".png", ".jpg", ".jpeg", ".webp")


def validate_workflow(wf, allowed: set[str]) -> dict:
    """Pure. Returns the workflow when safe, raises KitError(invalid_input) otherwise."""
    if not isinstance(wf, dict) or not wf or len(wf) > MAX_NODES:
        raise kitrt.KitError("invalid_input", "workflow must be an object with 1..%d nodes" % MAX_NODES)
    for node_id, node in wf.items():
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            raise kitrt.KitError("invalid_input", f"node {node_id}: missing class_type")
        if node["class_type"] not in allowed:
            raise kitrt.KitError("invalid_input", f"node {node_id}: class_type not allowed")
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            raise kitrt.KitError("invalid_input", f"node {node_id}: bad inputs")
        for key, value in inputs.items():
            if isinstance(value, str) and (".." in value or value.startswith("/") or "\\" in value or "://" in value):
                raise kitrt.KitError("invalid_input", f"node {node_id}: input {key} looks like a path or URL")
    return wf


def load_allowed() -> set[str]:
    with open(ALLOWED_NODES, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise kitrt.KitError("bad_asset_manifest", "allowed_nodes.json")
    return set(data)


def _http(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:  # loopback only
        return json.loads(r.read())


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    src = kitrt.input_file("workflow")
    if os.path.getsize(src) > MAX_WORKFLOW_BYTES:
        raise kitrt.KitError("invalid_input", "workflow too large")
    try:
        with open(src, "r", encoding="utf-8") as fh:
            wf = json.load(fh)
    except ValueError:
        raise kitrt.KitError("invalid_input", "workflow is not JSON") from None
    kitrt.verify_manifest(WEIGHTS, MANIFEST)
    validate_workflow(wf, load_allowed())

    run_dir = os.path.join(kitrt.OUT_DIR, ".comfy")
    os.makedirs(run_dir, exist_ok=True)
    out_dir = os.path.join(kitrt.OUT_DIR, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--listen", "127.0.0.1", "--port", str(PORT), "--output-directory", run_dir,
         "--disable-auto-launch"], cwd=COMFY_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + params["timeout_seconds"]
    try:
        while True:  # wait for the server
            if proc.poll() is not None:
                raise kitrt.KitError("server_failed", "ComfyUI exited at start")
            try:
                _http("GET", "/system_stats")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise kitrt.KitError("timeout", "server did not start") from None
                time.sleep(1)
        pid = _http("POST", "/prompt", {"prompt": wf}).get("prompt_id")
        if not isinstance(pid, str):
            raise kitrt.KitError("workflow_rejected", "server refused the workflow")
        while True:
            hist = _http("GET", f"/history/{pid}")
            if pid in hist:
                break
            if time.monotonic() > deadline or proc.poll() is not None:
                raise kitrt.KitError("timeout", "workflow did not finish")
            time.sleep(1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    entry = hist[pid]
    if (entry.get("status") or {}).get("status_str") not in (None, "success"):
        raise kitrt.KitError("workflow_failed", "ComfyUI reported an error")
    files = []
    for node_id in sorted(entry.get("outputs", {})):
        for img in entry["outputs"][node_id].get("images", []):
            name, sub = str(img.get("filename", "")), str(img.get("subfolder", ""))
            path = os.path.realpath(os.path.join(run_dir, sub, name))
            if not path.startswith(os.path.realpath(run_dir) + os.sep) or not name.lower().endswith(OUT_EXT) or not os.path.isfile(path):
                continue
            dest = f"{len(files):05d}_{os.path.basename(name)}"
            shutil.copyfile(path, os.path.join(out_dir, dest))
            files.append({"file": dest, "sha256": kitrt.sha256_file(os.path.join(out_dir, dest))})
            if len(files) >= params["max_outputs"]:
                break
    shutil.rmtree(run_dir, ignore_errors=True)
    if not files:
        raise kitrt.KitError("no_outputs", "the workflow produced no images")
    return {"kit": "comfyui-workflow-runner", "outputs": files}


if __name__ == "__main__":
    kitrt.run_main(main)
