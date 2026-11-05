"""ComputeCoin text-generation workload (executor contract).

Contract (provider-agent executor):
  COMPUTECOIN_INPUT  path of input.json   {"prompt": str, "messages": [{role, content}]?, "max_tokens": int?}
  COMPUTECOIN_OUTPUT path to write output.json {"text": str, "tokens": int, "prompt_tokens": int, ...}
  COMPUTECOIN_JOB_ID, COMPUTECOIN_MODEL, COMPUTECOIN_WORKLOAD_TYPE

Streaming contract (B2, optional; the agent sets these when it can relay tokens):
  COMPUTECOIN_STREAM    path of an append-only NDJSON file. Per generated piece the workload appends one
                        line {"text": "<new text>", "final": false}; the last line is {"text": "", "final": true}.
                        The agent tails it, numbers the chunks and POSTs them to /v1/agents/jobs/{id}/stream.
                        Private tiers (sealed, sealed_pro, confidential): the path is a named pipe (FIFO) on RAM-only storage, not
                        a file, so no plaintext stream ever reaches disk. The contract is unchanged as long as the workload
                        opens the path for append PER WRITE (as StreamWriter does), writes whole lines, never seeks, never
                        reads it back and treats a write error as best-effort. The agent seals each chunk in-process.
  COMPUTECOIN_PROGRESS  path of progress.json {"progress_pct": 0-100, "step_count": int, "phase": str},
                        replaced atomically. Both are best-effort; output.json stays the source of truth.

Determinism (R12): greedy decoding (no sampling), fixed seed, no network at run time.
The weights live in the image at MODEL_DIR; their SHA-256 manifest is checked before use.
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
MAX_TOKENS_CAP = 2048
DEFAULT_MAX_TOKENS = 256


class WorkloadError(Exception):
    pass


def verify_manifest(model_dir: str, manifest_path: str) -> str:
    """Check every weight file against the SHA-256 recorded at build time; return the manifest digest."""
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


def read_input(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise WorkloadError("input must be a JSON object")
    return data


def normalise(data: dict[str, Any]) -> tuple[list[dict[str, str]], int]:
    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        msgs = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
            if isinstance(m, dict)
        ]
    else:
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise WorkloadError("input.prompt must be a non-empty string")
        msgs = [{"role": "user", "content": prompt}]
    temperature = data.get("temperature")  # accepted for the chat contract; decoding stays greedy (R12)
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))):
        raise WorkloadError("input.temperature must be a number")
    max_tokens = data.get("max_tokens", DEFAULT_MAX_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise WorkloadError("input.max_tokens must be a positive integer")
    return msgs, min(max_tokens, MAX_TOKENS_CAP)


Backend = Callable[[list[dict[str, str]], int], tuple[str, int, int]]


class StreamWriter:
    """Appends NDJSON chunks and an atomic progress file (the agent tails them). Never raises."""

    def __init__(self, stream_path: str | None, progress_path: str | None) -> None:
        self.stream_path, self.progress_path = stream_path, progress_path
        self.steps = 0

    def piece(self, text: str, max_tokens: int = 0) -> None:
        self.steps += 1
        try:
            if self.stream_path and text:
                with open(self.stream_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"text": text, "final": False}, ensure_ascii=False) + "\n")
            if self.progress_path and max_tokens:
                tmp = self.progress_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"progress_pct": min(99.0, 100.0 * self.steps / max_tokens),
                               "step_count": self.steps, "phase": "generating"}, fh)
                os.replace(tmp, self.progress_path)
        except OSError:
            pass

    def finish(self) -> None:
        try:
            if self.stream_path:
                with open(self.stream_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"text": "", "final": True}) + "\n")
        except OSError:
            pass


def transformers_backend(model_dir: str, writer: "StreamWriter | None" = None) -> Backend:  # pragma: no cover - needs weights and a GPU
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.manual_seed(SEED)
    torch.use_deterministic_algorithms(True, warn_only=True)
    tok = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True, torch_dtype=torch.float16)
    if torch.cuda.is_available():  # ROCm builds of torch also report cuda
        model = model.to("cuda")
    model.eval()

    def generate(messages: list[dict[str, str]], max_tokens: int) -> tuple[str, int, int]:
        torch.manual_seed(SEED)
        if getattr(tok, "chat_template", None):
            # messages present: the model's own chat template (generation prompt appended)
            ids = tok.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt").to(model.device)
        else:  # a base model without a template: plain "role: content" rendering, same as the contract's `prompt`
            text_prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages) + "\nassistant:"
            ids = tok(text_prompt, return_tensors="pt").input_ids.to(model.device)
        with torch.no_grad():
            streamer = None
            if writer is not None:
                from transformers.generation.streamers import BaseStreamer

                class _Relay(BaseStreamer):
                    def __init__(self) -> None:
                        self.ids: list[int] = []
                        self.sent = ""
                        self.first = True

                    def put(self, value: Any) -> None:
                        if self.first:  # the first call is the prompt itself
                            self.first = False
                            return
                        self.ids.extend(int(t) for t in value.reshape(-1).tolist())
                        text = tok.decode(self.ids, skip_special_tokens=True)
                        if text.startswith(self.sent) and len(text) > len(self.sent) and not text.endswith("\ufffd"):
                            writer.piece(text[len(self.sent):], max_tokens)
                            self.sent = text

                    def end(self) -> None:
                        text = tok.decode(self.ids, skip_special_tokens=True)
                        if text.startswith(self.sent) and len(text) > len(self.sent):
                            writer.piece(text[len(self.sent):], max_tokens)
                            self.sent = text

                streamer = _Relay()
            out = model.generate(ids, max_new_tokens=max_tokens, do_sample=False, num_beams=1, streamer=streamer)
        new = out[0][ids.shape[1]:]
        return tok.decode(new, skip_special_tokens=True), int(ids.shape[1]), int(new.shape[0])

    return generate


def run(env: dict[str, str] | None = None, backend: Backend | None = None, verify: bool = True) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    try:
        in_path = env["COMPUTECOIN_INPUT"]
        out_path = env["COMPUTECOIN_OUTPUT"]
    except KeyError as exc:
        raise WorkloadError(f"missing {exc.args[0]}") from exc
    workload = env.get("COMPUTECOIN_WORKLOAD_TYPE", "text-generation")
    if workload != "text-generation":
        raise WorkloadError(f"unsupported workload type {workload}")
    messages, max_tokens = normalise(read_input(in_path))
    manifest_digest = verify_manifest(MODEL_DIR, MANIFEST) if verify else None
    writer = StreamWriter(env.get("COMPUTECOIN_STREAM"), env.get("COMPUTECOIN_PROGRESS"))
    if backend is None:
        backend = transformers_backend(MODEL_DIR, writer)
    text, prompt_tokens, completion_tokens = backend(messages, max_tokens)
    writer.finish()
    output = {
        "text": text,
        "tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
        "model": env.get("COMPUTECOIN_MODEL", ""),
        "execution_mode": "hardware",
        "simulated": False,
        "decoding": {"strategy": "greedy", "seed": SEED, "max_tokens": max_tokens},
        "weights_manifest_sha256": manifest_digest,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(output, fh, sort_keys=True, ensure_ascii=False)
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
