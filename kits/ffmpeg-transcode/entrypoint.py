"""ffmpeg-transcode Kit entrypoint (container contract: kitrt).

Encoder choice: `auto` tries NVENC with a 0.1 s probe encode and falls back to the CPU encoder (libx264/libx265,
present only in a GPL ffmpeg build). `nvenc` and `cpu` are strict (no silent fallback).
Safety: only the `file` and `pipe` protocols are allowed on input, so a playlist cannot make ffmpeg read other
files or open sockets. Metadata is stripped. No resume: a retry re-encodes (fan-out by segments is U4).
ffmpeg flags to verify against current docs for the pinned build (L4): -protocol_whitelist, -c:v h264_nvenc,
-rc vbr -cq, -preset p1..p7, -c:v libx264 -crf -preset, -map_metadata -1, -movflags +faststart, -ss/-t.
"""

from __future__ import annotations

import os
import subprocess

import kitrt

FFMPEG = os.environ.get("FFMPEG_BIN", "/opt/ffmpeg/ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "/opt/ffmpeg/ffprobe")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
TIMEOUT_S = int(os.environ.get("ENCODE_TIMEOUT_S", "43200"))

NVENC = {"h264": "h264_nvenc", "h265": "hevc_nvenc"}
CPU = {"h264": "libx264", "h265": "libx265"}
NVENC_PRESET = {"fast": "p3", "medium": "p4", "slow": "p6"}


def build_args(params: dict, source: str, dest: str, encoder: str) -> list[str]:
    """Full ffmpeg argv for one encode. `encoder` is 'nvenc' or 'cpu'. Pure."""
    a = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe"]
    if params["start_seconds"]:
        a += ["-ss", f"{params['start_seconds']:.3f}"]
    a += ["-i", source]
    if "duration_seconds" in params:
        a += ["-t", f"{params['duration_seconds']:.3f}"]
    a += ["-map", "0:v:0"]
    a += ["-map", "0:a:0?"] if params["audio"] == "aac" else ["-an"]
    if "height" in params:
        # -2 keeps the aspect ratio with an even width.
        a += ["-vf", f"scale=-2:{params['height']}"]
    if encoder == "nvenc":
        a += ["-c:v", NVENC[params["codec"]], "-preset", NVENC_PRESET[params["preset"]], "-rc", "vbr",
              "-cq", str(params["crf"]), "-b:v", "0"]
    else:
        a += ["-c:v", CPU[params["codec"]], "-crf", str(params["crf"]), "-preset", params["preset"]]
    a += ["-pix_fmt", "yuv420p"]
    if params["codec"] == "h265":
        a += ["-tag:v", "hvc1"]
    if params["audio"] == "aac":
        a += ["-c:a", "aac", "-b:a", "128k"]
    a += ["-map_metadata", "-1", "-movflags", "+faststart", dest]
    return a


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False, shell=False)


def _has_encoder(name: str) -> bool:
    p = _run([FFMPEG, "-hide_banner", "-encoders"], 30)
    return p.returncode == 0 and name.encode() in p.stdout


def nvenc_works(codec: str) -> bool:
    """True when the encoder exists in this build AND a tiny real encode succeeds on this host's GPU."""
    enc = NVENC[codec]
    if not _has_encoder(enc):
        return False
    p = _run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
              "-c:v", enc, "-f", "null", "-"], 60)
    return p.returncode == 0


def choose_encoder(params: dict) -> str:
    want = params["encoder"]
    if want in ("auto", "nvenc") and nvenc_works(params["codec"]):
        return "nvenc"
    if want == "nvenc":
        raise kitrt.KitError("encoder_unavailable", "NVENC is not usable on this host")
    if not _has_encoder(CPU[params["codec"]]):
        raise kitrt.KitError("encoder_unavailable", f"{CPU[params['codec']]} is not in this ffmpeg build (LGPL build)")
    return "cpu"


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    source = kitrt.input_file("source")
    dest = os.path.join(kitrt.OUT_DIR, "result.mp4")
    encoder = choose_encoder(params)
    proc = _run(build_args(params, source, dest, encoder), TIMEOUT_S)
    if proc.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise kitrt.KitError("encode_failed", proc.stderr[-600:].decode("utf-8", "replace"))
    probe = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                  "-of", "json", "--", dest], 60)
    return {"kit": "ffmpeg-transcode", "encoder": encoder, "codec": params["codec"],
            "output_sha256": kitrt.sha256_file(dest), "output_bytes": os.path.getsize(dest),
            "probe": probe.stdout.decode("utf-8", "replace")[:2000]}


if __name__ == "__main__":
    kitrt.run_main(main)
