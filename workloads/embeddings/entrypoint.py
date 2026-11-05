"""ComputeCoin embeddings workload (executor contract).

COMPUTECOIN_INPUT  input.json  {"input": [str, ...]}
COMPUTECOIN_OUTPUT output.json {"embeddings": [[float]], "dim": int, "tokens": int, ...}
Deterministic: eval mode, no sampling, fixed seed, L2-normalised, no network at run time.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, Callable

MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/model")
MANIFEST = os.environ.get("MODEL_MANIFEST", "/opt/model.sha256")
SEED = 1234
MAX_ITEMS = 100


class WorkloadError(Exception):
    pass


def verify_manifest(model_dir: str, manifest_path: str) -> str:
    with open(manifest_path, "rb") as fh:
        raw = fh.read()
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        h = hashlib.sha256()
        with open(os.path.join(model_dir, name), "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != digest:
            raise WorkloadError(f"weight file {name} does not match its recorded SHA-256")
    return hashlib.sha256(raw).hexdigest()


Backend = Callable[[list[str]], tuple[list[list[float]], int]]


def transformers_backend(model_dir: str) -> Backend:  # pragma: no cover - needs weights and a GPU
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.manual_seed(SEED)
    tok = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    model = AutoModel.from_pretrained(model_dir, local_files_only=True)
    if torch.cuda.is_available():
        model = model.to("cuda")
    model.eval()

    def embed(texts: list[str]) -> tuple[list[list[float]], int]:
        enc = tok(texts, padding=True, truncation=True, max_length=512, return_tensors="pt").to(model.device)
        with torch.no_grad():
            hidden = model(**enc).last_hidden_state
        mask = enc["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
        pooled = torch.nn.functional.normalize(pooled, dim=1)
        return [[round(float(x), 6) for x in row] for row in pooled.cpu()], int(enc["attention_mask"].sum())

    return embed


def run(env: dict[str, str] | None = None, backend: Backend | None = None, verify: bool = True) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    try:
        in_path = env["COMPUTECOIN_INPUT"]
        out_path = env["COMPUTECOIN_OUTPUT"]
    except KeyError as exc:
        raise WorkloadError(f"missing {exc.args[0]}") from exc
    if env.get("COMPUTECOIN_WORKLOAD_TYPE", "embeddings") != "embeddings":
        raise WorkloadError("unsupported workload type")
    with open(in_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    texts = data.get("input") if isinstance(data, dict) else None
    if not isinstance(texts, list) or not texts or len(texts) > MAX_ITEMS or not all(isinstance(t, str) for t in texts):
        raise WorkloadError("input.input must be a non-empty list of strings (max 100)")
    manifest_digest = verify_manifest(MODEL_DIR, MANIFEST) if verify else None
    if backend is None:
        backend = transformers_backend(MODEL_DIR)
    vectors, tokens = backend(texts)
    output = {
        "embeddings": vectors,
        "dim": len(vectors[0]) if vectors else 0,
        "tokens": tokens,
        "model": env.get("COMPUTECOIN_MODEL", ""),
        "execution_mode": "hardware",
        "simulated": False,
        "weights_manifest_sha256": manifest_digest,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(output, fh, sort_keys=True)
    os.replace(tmp, out_path)
    return output


def main() -> int:
    try:
        run()
    except WorkloadError as exc:
        print(f"workload error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
