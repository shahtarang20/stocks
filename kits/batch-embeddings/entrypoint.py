"""batch-embeddings Kit entrypoint: one baked encoder, transformers + mean/CLS pooling (container contract: kitrt).

Fixed batch order (input order, no length sorting) so a batch never depends on its neighbours across runs. Values are
rounded to `round_decimals` before writing. Checkpoint: one .npy chunk per CHUNK rows + COMMIT.
transformers API to verify against current docs before use (L4): AutoTokenizer.from_pretrained(dir),
AutoModel.from_pretrained(dir), tokenizer(list, padding=True, truncation=True, max_length=, return_tensors="pt").
"""

from __future__ import annotations

import json
import os

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
MAX_TEXTS = 1_000_000
MAX_BYTES = 1024**3
MAX_TEXT_BYTES = 1024 * 1024
CHUNK = 2048


def parse_texts(path: str) -> tuple[list[str], list]:
    """JSONL {"id","text"} or plain lines. Returns (texts, ids). Refuses oversize or mixed input. Pure given the file."""
    if os.path.getsize(path) > MAX_BYTES:
        raise kitrt.KitError("invalid_input", "texts file too large")
    texts: list[str] = []
    ids: list = []
    with open(path, "r", encoding="utf-8", errors="strict") as fh:
        for n, line in enumerate(fh):
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_TEXT_BYTES + 256:
                raise kitrt.KitError("invalid_input", f"line {n + 1} too long")
            text, ident = line, len(texts)
            if line.lstrip().startswith("{"):
                try:
                    obj = json.loads(line)
                    text, ident = obj["text"], obj.get("id", len(texts))
                except (ValueError, KeyError, TypeError):
                    raise kitrt.KitError("invalid_input", f"line {n + 1} is not a valid record") from None
                if not isinstance(text, str) or isinstance(ident, (dict, list)):
                    raise kitrt.KitError("invalid_input", f"line {n + 1} has bad field types")
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise kitrt.KitError("invalid_input", f"text {len(texts)} too long")
            texts.append(text)
            ids.append(ident)
            if len(texts) > MAX_TEXTS:
                raise kitrt.KitError("invalid_input", "too many texts")
    if not texts:
        raise kitrt.KitError("invalid_input", "no texts")
    return texts, ids


def pool(hidden, mask, mode: str):
    """hidden: BxTxD tensor, mask: BxT. mean = masked average, cls = first token."""
    if mode == "cls":
        return hidden[:, 0]
    m = mask.unsqueeze(-1).to(hidden.dtype)
    return (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    texts, ids = parse_texts(kitrt.input_file("texts"))
    out_dir = os.path.join(kitrt.OUT_DIR, "embeddings")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(kitrt.CKPT_DIR, exist_ok=True)
    kitrt.verify_manifest(WEIGHTS, MANIFEST)

    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.use_deterministic_algorithms(True, warn_only=True)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    if not torch.cuda.is_available():
        raise kitrt.KitError("gpu_unavailable", "no CUDA device visible")
    model_dir = os.path.join(WEIGHTS, "model")
    tok = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    model = AutoModel.from_pretrained(model_dir, local_files_only=True, torch_dtype=torch.float32).to("cuda").eval()

    done = kitrt.committed_files()
    committed = list(done.values())
    seq = len(done)
    chunk_paths = []
    for c0 in range(0, len(texts), CHUNK):
        rel = f"chunk_{c0 // CHUNK:06d}.npy"
        ck = os.path.join(kitrt.CKPT_DIR, rel)
        if rel not in done:
            rows = []
            for b0 in range(c0, min(c0 + CHUNK, len(texts)), params["batch_size"]):
                batch = texts[b0:min(b0 + params["batch_size"], c0 + CHUNK, len(texts))]
                enc = tok(batch, padding=True, truncation=True, max_length=params["max_length"], return_tensors="pt").to("cuda")
                with torch.inference_mode():
                    vec = pool(model(**enc).last_hidden_state.float(), enc["attention_mask"], params["pooling"])
                    if params["normalize"]:
                        vec = torch.nn.functional.normalize(vec, p=2, dim=1)
                rows.append(vec.cpu().numpy())
            arr = np.round(np.concatenate(rows, axis=0), params["round_decimals"]).astype(np.float32)
            with open(ck + ".part", "wb") as fh:
                np.save(fh, arr)
            os.replace(ck + ".part", ck)
            committed.append(ck)
            seq += 1
            kitrt.commit(committed, seq)
        chunk_paths.append(ck if rel not in done else done[rel])
    matrix = np.concatenate([np.load(p) for p in chunk_paths], axis=0)
    if matrix.shape[0] != len(texts):
        raise kitrt.KitError("internal_error", "row count mismatch")
    out_npy = os.path.join(out_dir, "embeddings.npy")
    np.save(out_npy, matrix)
    with open(os.path.join(out_dir, "ids.json"), "w", encoding="utf-8") as fh:
        json.dump(ids, fh, ensure_ascii=False)
    return {"kit": "batch-embeddings", "count": len(texts), "dim": int(matrix.shape[1]), "resumed_chunks": len(done),
            "embeddings_sha256": kitrt.sha256_file(out_npy)}


if __name__ == "__main__":
    kitrt.run_main(main)
