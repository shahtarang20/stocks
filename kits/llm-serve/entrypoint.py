"""llm-serve Kit entrypoint: vLLM OpenAI-compatible server over baked weights, run as a U11 Service replica.

Service contract (provider-agent/computecoin_agent/service_agent.py, DockerServiceContainer): the agent starts the image with
NO command, NO env, NO mounts (only a /tmp tmpfs), `--user 65532:65532`, `--read-only`, on an `--internal` docker network, then
reaches it at http://<container ip>:<service port>. Therefore:
  * the server MUST bind 0.0.0.0, not 127.0.0.1: loopback inside the container is unreachable from the agent. No port is
    published and the network has no egress, so the only peer that can connect is the agent;
  * port 8000 (the agent default) and health path /health must match the Service record (port, health_path);
  * nothing arrives through input.json or COMPUTECOIN_* variables, so every setting is a constant here.
vLLM flag names and the /health route: verify against the vLLM docs for the pinned version before use (L4).
"""

from __future__ import annotations

import os
import sys

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
PORT = 8000
HOST = "0.0.0.0"  # reached by container IP on an internal network; see module docstring
HEALTH_PATH = "/health"
SERVED_MODEL_NAME = "model"
MAX_MODEL_LEN = 8192
MAX_NUM_SEQS = 64
GPU_MEMORY_UTILIZATION = 0.90


def build_argv(model_dir: str) -> list[str]:
    """The exact vLLM command. Pure; no flag enables remote code, a network fetch or an API key from outside."""
    return [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", model_dir, "--served-model-name", SERVED_MODEL_NAME,
        "--host", HOST, "--port", str(PORT),
        "--max-model-len", str(MAX_MODEL_LEN), "--max-num-seqs", str(MAX_NUM_SEQS),
        "--gpu-memory-utilization", str(GPU_MEMORY_UTILIZATION),
        "--dtype", "float16", "--seed", "0", "--enforce-eager",
        "--disable-log-requests",
    ]


def child_env(env: dict[str, str]) -> dict[str, str]:
    """Offline, no telemetry, every cache on the /tmp tmpfs (the rest of the filesystem is read-only)."""
    out = dict(env)
    out.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1",
        "HOME": "/tmp", "HF_HOME": "/tmp/hf", "VLLM_CACHE_ROOT": "/tmp/vllm", "XDG_CACHE_HOME": "/tmp/cache",
        "TRITON_CACHE_DIR": "/tmp/triton", "TORCHINDUCTOR_CACHE_DIR": "/tmp/inductor", "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
    })
    return out


def main() -> None:
    model_dir = os.path.join(WEIGHTS, "model")
    if not os.path.isdir(model_dir):
        raise kitrt.KitError("bad_asset_manifest", "model directory missing")
    kitrt.verify_manifest(WEIGHTS, MANIFEST)  # hash-check the baked weights before serving anything
    os.execve(sys.executable, build_argv(model_dir), child_env(dict(os.environ)))


if __name__ == "__main__":
    try:
        main()
    except kitrt.KitError as exc:
        print(f"kit error {exc.code}: {exc}", file=sys.stderr)
        sys.exit(2)
