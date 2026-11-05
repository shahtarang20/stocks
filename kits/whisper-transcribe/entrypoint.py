"""whisper-transcribe Kit entrypoint: faster-whisper (CTranslate2) over baked Whisper weights (container contract: kitrt).

Input `audio`: one audio or video file. Temperature is fixed at 0 (no fallback sampling) and the compute type is fixed,
so the only variation left is across GPU models; verification therefore needs a word-error-rate bound (owner decision).
faster-whisper API to verify against current docs before use (L4): WhisperModel(model_path, device=, compute_type=);
model.transcribe(path, language=, task=, beam_size=, temperature=, word_timestamps=, vad_filter=) returns (segments, info),
segments is a generator of objects with .start .end .text and .words (when word_timestamps).
"""

from __future__ import annotations

import json
import os

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
MAX_INPUT_BYTES = 4 * 1024**3
MAX_SEGMENTS = 200_000


def fmt_ts(seconds: float, sep: str) -> str:
    """HH:MM:SS<sep>mmm with integer millisecond rounding. Pure."""
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(segs: list[dict]) -> str:
    return "".join(f"{i}\n{fmt_ts(s['start'], ',')} --> {fmt_ts(s['end'], ',')}\n{s['text'].strip()}\n\n"
                   for i, s in enumerate(segs, 1))


def to_vtt(segs: list[dict]) -> str:
    body = "".join(f"{fmt_ts(s['start'], '.')} --> {fmt_ts(s['end'], '.')}\n{s['text'].strip()}\n\n" for s in segs)
    return "WEBVTT\n\n" + body


def to_txt(segs: list[dict]) -> str:
    return "\n".join(s["text"].strip() for s in segs) + "\n"


def render(fmt: str, segs: list[dict], language: str | None, duration: float | None) -> str:
    if fmt == "json":
        return json.dumps({"language": language, "duration": duration, "segments": segs}, sort_keys=True, ensure_ascii=False)
    return {"srt": to_srt, "vtt": to_vtt, "txt": to_txt}[fmt](segs)


def segment_dict(seg, words: bool) -> dict:
    d = {"start": round(float(seg.start), 3), "end": round(float(seg.end), 3), "text": str(seg.text)}
    if words and getattr(seg, "words", None):
        d["words"] = [{"start": round(float(w.start), 3), "end": round(float(w.end), 3), "word": str(w.word)} for w in seg.words]
    return d


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    source = kitrt.input_file("audio")
    size = os.path.getsize(source)
    if size == 0 or size > MAX_INPUT_BYTES:
        raise kitrt.KitError("invalid_input", "audio file empty or too large")
    out_dir = os.path.join(kitrt.OUT_DIR, "transcripts")
    os.makedirs(out_dir, exist_ok=True)
    kitrt.verify_manifest(WEIGHTS, MANIFEST)

    from faster_whisper import WhisperModel

    model_dir = os.path.join(WEIGHTS, "model")
    if not os.path.isdir(model_dir):
        raise kitrt.KitError("bad_asset_manifest", "model directory missing")
    model = WhisperModel(model_dir, device="cuda", compute_type="float16")  # no silent CPU fallback: fail loudly
    language = None if params["language"] == "auto" else params["language"]
    try:
        segments, info = model.transcribe(
            source, language=language, task=params["task"], beam_size=params["beam_size"], temperature=0.0,
            word_timestamps=params["word_timestamps"], vad_filter=params["vad_filter"], condition_on_previous_text=False)
        segs: list[dict] = []
        for seg in segments:
            segs.append(segment_dict(seg, params["word_timestamps"]))
            if len(segs) > MAX_SEGMENTS:
                raise kitrt.KitError("invalid_input", "too many segments")
    except kitrt.KitError:
        raise
    except Exception as exc:  # undecodable media is the customer's input error, not an internal one
        raise kitrt.KitError("invalid_input", f"could not transcribe: {type(exc).__name__}") from exc
    lang = getattr(info, "language", None)
    duration = getattr(info, "duration", None)
    files = []
    for fmt in dict.fromkeys(params["formats"]):  # unique, in order
        path = os.path.join(out_dir, f"transcript.{fmt}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render(fmt, segs, lang, duration))
        files.append({"file": f"transcript.{fmt}", "sha256": kitrt.sha256_file(path)})
    return {"kit": "whisper-transcribe", "language": lang, "duration_seconds": duration, "segments": len(segs), "files": files}


if __name__ == "__main__":
    kitrt.run_main(main)
