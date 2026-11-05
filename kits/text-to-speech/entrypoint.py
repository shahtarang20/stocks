"""text-to-speech Kit entrypoint: Kokoro-82M over baked weights, one WAV per input text (container contract: kitrt).

Output is 24 kHz mono 16-bit PCM written with the standard-library `wave` module (no timestamps or metadata), so the file
is a pure function of the samples. Float results can differ slightly across GPU models, so verification is the
`audio_spectral` plug-in (log-mel cosine), not byte-exact. Checkpoint: after each text the WAV is written to the checkpoint
directory and COMMIT is replaced.
Library calls to verify against current docs before use (L4): kokoro.KModel(config=, model=, repo_id=) and
kokoro.KPipeline(lang_code=, model=, repo_id=); pipeline(text, voice=<name or path to .pt>, speed=, split_pattern=) yields
(graphemes, phonemes, audio) with audio a 24 kHz float tensor; the phonemiser may need espeak-ng in the base image.
"""

from __future__ import annotations

import os
import shutil
import wave

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
SAMPLE_RATE = 24000
MAX_SECONDS_PER_TEXT = 600
LANG_BY_PREFIX = {"a": "a", "b": "b"}  # af_/am_ American English, bf_/bm_ British English


def plan(params: dict) -> list[dict]:
    """Per-text plan; pure, so it can be tested without a GPU."""
    return [{"index": i, "text": t, "file": f"speech_{i:05d}.wav"} for i, t in enumerate(params["texts"])]


def lang_code(voice: str) -> str:
    code = LANG_BY_PREFIX.get(voice[:1])
    if code is None:
        raise kitrt.KitError("invalid_param", "voice has no known language")
    return code


def pcm16(samples) -> bytes:
    """Float samples in [-1, 1] to little-endian 16-bit PCM bytes (round half away via numpy rint, clipped). Pure."""
    import numpy as np

    x = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    return np.rint(x * 32767.0).astype("<i2").tobytes()


def write_wav(path: str, samples) -> None:
    data = pcm16(samples)
    if not data:
        raise kitrt.KitError("synthesis_failed", "empty audio")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(data)


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    items = plan(params)
    voice_path = os.path.join(WEIGHTS, "voices", f"{params['voice']}.pt")
    out_dir = os.path.join(kitrt.OUT_DIR, "audio")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(kitrt.CKPT_DIR, exist_ok=True)
    manifest_digest = kitrt.verify_manifest(WEIGHTS, MANIFEST)
    if not os.path.isfile(voice_path):
        raise kitrt.KitError("bad_asset_manifest", "voice file missing")

    done = kitrt.committed_files()
    todo = [it for it in items if f"audio/{it['file']}" not in done]
    pipeline = None
    if todo:
        import numpy as np
        import torch
        from kokoro import KModel, KPipeline

        if not torch.cuda.is_available():
            raise kitrt.KitError("gpu_unavailable", "no CUDA device")  # no silent CPU fallback
        torch.use_deterministic_algorithms(True, warn_only=True)
        model = KModel(config=os.path.join(WEIGHTS, "config.json"), model=os.path.join(WEIGHTS, "kokoro.pth")).to("cuda").eval()
        pipeline = KPipeline(lang_code=lang_code(params["voice"]), model=model)
    seq = len(done)
    committed: list[str] = list(done.values())
    total_samples = 0
    for it in items:
        rel = f"audio/{it['file']}"
        ck = os.path.join(kitrt.CKPT_DIR, rel)
        if rel in done:
            shutil.copyfile(done[rel], os.path.join(out_dir, it["file"]))
            continue
        chunks = []
        for _, _, audio in pipeline(it["text"], voice=voice_path, speed=params["speed"], split_pattern=r"\n+"):
            if audio is not None:
                chunks.append(audio.detach().cpu().numpy().astype(np.float64))
            if sum(c.size for c in chunks) > SAMPLE_RATE * MAX_SECONDS_PER_TEXT:
                raise kitrt.KitError("invalid_input", "text produces too much audio")
        if not chunks:
            raise kitrt.KitError("synthesis_failed", f"no audio for text {it['index']}")
        os.makedirs(os.path.dirname(ck), exist_ok=True)
        write_wav(ck + ".part", np.concatenate(chunks))
        os.replace(ck + ".part", ck)
        committed.append(ck)
        seq += 1
        kitrt.commit(committed, seq)
        shutil.copyfile(ck, os.path.join(out_dir, it["file"]))

    results = []
    for it in items:
        path = os.path.join(out_dir, it["file"])
        with wave.open(path, "rb") as w:
            total_samples += w.getnframes()
        results.append({"index": it["index"], "file": it["file"], "sha256": kitrt.sha256_file(path)})
    return {"kit": "text-to-speech", "weights_manifest_sha256": manifest_digest, "voice": params["voice"],
            "sample_rate": SAMPLE_RATE, "duration_seconds": round(total_samples / SAMPLE_RATE, 3), "files": results,
            "resumed_files": len(done)}


if __name__ == "__main__":
    kitrt.run_main(main)
