"""PSNR / SSIM check for video encodes against a reference re-encode of a sampled window.

``primary`` and ``reference`` are each a single video file (the host's encode of the window, and an
independent host's encode of the same window with the same parameters). The plug-in
  1. probes both with ffprobe (duration, stream layout, resolution, frame count),
  2. decodes up to ``max_frames`` evenly spaced frames to small grayscale arrays,
  3. compares PSNR and SSIM per frame.
A truncated encode fails the duration/frame-count checks; a low-bitrate encode fails PSNR/SSIM.

ffmpeg/ffprobe must exist on the verification worker. If not, the outcome is ``inconclusive``.
The metric core (:func:`compare_frame_sequences`) is pure numpy so it is testable without ffmpeg.
Commands are argv lists, never a shell. Library and tool flags to verify against current docs (L4):
ffprobe ``-print_format json -show_streams -show_format``; ffmpeg ``-f rawvideo -pix_fmt gray``;
skimage.metrics.structural_similarity.
Thresholds are conservative defaults and an OWNER DECISION per kit.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import FAILED, INCONCLUSIVE, PASSED, ThresholdError, VerifyOutcome, resolve_thresholds

MAX_FILE_BYTES = 16 * 1024**3
PROBE_TIMEOUT_S = 60
DECODE_TIMEOUT_S = 600
ANALYSIS_WIDTH = 640  # frames are scaled to this width (height keeps aspect, even) before metrics


class _Tool(Exception):
    pass


def psnr(a, b) -> float:
    import numpy as np

    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    if mse == 0.0:
        return 100.0  # identical; capped so averages stay finite
    return min(100.0, 10.0 * math.log10(255.0 * 255.0 / mse))


def compare_frame_sequences(frames_a: list, frames_b: list, th: dict[str, float]) -> dict[str, Any]:
    """Compare two equal-length lists of 2-D uint8 arrays. Pure; no I/O."""
    from skimage.metrics import structural_similarity

    n = min(len(frames_a), len(frames_b))
    if n == 0:
        return {"frames": 0, "mean_psnr": 0.0, "min_psnr": 0.0, "mean_ssim": 0.0, "min_ssim": 0.0}
    ps, ss = [], []
    for fa, fb in zip(frames_a[:n], frames_b[:n]):
        if fa.shape != fb.shape:
            return {"frames": n, "mean_psnr": 0.0, "min_psnr": 0.0, "mean_ssim": 0.0, "min_ssim": 0.0,
                    "shape_mismatch": True}
        ps.append(psnr(fa, fb))
        ss.append(float(structural_similarity(fa, fb, data_range=255)) if min(fa.shape) >= 7 else 1.0)
    return {"frames": n, "mean_psnr": round(sum(ps) / n, 3), "min_psnr": round(min(ps), 3),
            "mean_ssim": round(sum(ss) / n, 5), "min_ssim": round(min(ss), 5)}


def _run(argv: list[str], timeout: int) -> bytes:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _Tool(type(exc).__name__)
    if proc.returncode != 0:
        raise _Tool("tool_exit_%d" % proc.returncode)
    return proc.stdout


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    raw = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                "-count_frames", "--", str(path)], PROBE_TIMEOUT_S)
    data = json.loads(raw.decode("utf-8", "replace"))
    video = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if len(video) != 1:
        return {"video_streams": len(video), "audio_streams": len(audio)}
    v = video[0]

    def num(x):
        try:
            f = float(x)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None  # NaN would make every ``abs(a - b) > limit`` comparison False

    return {"video_streams": 1, "audio_streams": len(audio), "width": v.get("width"), "height": v.get("height"),
            "frames": int(v["nb_read_frames"]) if str(v.get("nb_read_frames", "")).isdigit() else None,
            "duration": num((data.get("format") or {}).get("duration")) or num(v.get("duration"))}


def decode_gray_frames(path: Path, ffmpeg: str, width: int, height: int, total_frames: int | None,
                       max_frames: int) -> list:
    """Decode up to ``max_frames`` evenly spaced frames, scaled to width x height, as gray arrays."""
    import numpy as np

    step = max(1, (total_frames or max_frames) // max_frames)
    vf = f"select='not(mod(n\\,{step}))',scale={width}:{height}:flags=bicubic,format=gray"
    raw = _run([ffmpeg, "-v", "error", "-i", str(path), "-vf", vf, "-vsync", "0", "-frames:v", str(max_frames),
                "-f", "rawvideo", "-pix_fmt", "gray", "-"], DECODE_TIMEOUT_S)
    size = width * height
    count = len(raw) // size
    arr = np.frombuffer(raw[: count * size], dtype=np.uint8).reshape(count, height, width)
    return [arr[i] for i in range(count)]


class PsnrSsimPlugin:
    method = "psnr_ssim"
    # OWNER DECISION. Conservative: hardware (NVENC) and software encodes legitimately differ, so the
    # bounds are loose enough for an honest cross-encoder re-run but fail truncated or starved encodes.
    default_thresholds: dict[str, float] = {
        "min_mean_psnr_db": 30.0,
        "min_frame_psnr_db": 24.0,
        "min_mean_ssim": 0.90,
        "max_duration_diff_s": 0.5,
        "max_frame_count_diff": 2,
        "max_frames_analysed": 48,
    }
    threshold_bounds: dict[str, tuple[float, float]] = {
        "min_mean_psnr_db": (15.0, 60.0), "min_frame_psnr_db": (10.0, 60.0), "min_mean_ssim": (0.5, 1.0),
        "max_duration_diff_s": (0.0, 5.0), "max_frame_count_diff": (0, 30), "max_frames_analysed": (4, 200),
    }

    def verify(self, primary: list[Path], reference: list[Path], thresholds: dict[str, float] | None = None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE)
        try:
            th = resolve_thresholds(self.default_thresholds, self.threshold_bounds, thresholds)
        except ThresholdError as exc:
            out.reason = f"invalid_thresholds:{exc}"
            return out
        out.thresholds = th
        if not reference or len(reference) != 1:
            out.reason = "reference_missing"
            return out
        if len(primary) != 1:
            out.status, out.reason = FAILED, "primary_output_missing"
            return out
        pa, pb = primary[0], reference[0]
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            out.reason = "ffmpeg_not_available"
            return out
        try:
            import numpy  # noqa: F401
            import skimage  # noqa: F401
        except ImportError:
            out.reason = "verification_dependencies_missing"
            return out
        try:
            if pa.stat().st_size > MAX_FILE_BYTES or pb.stat().st_size > MAX_FILE_BYTES:
                out.reason = "file_too_large"
                return out
            if pa.stat().st_size == 0:
                out.status, out.reason = FAILED, "primary_output_empty"
                return out
            try:
                info_b = probe(pb, ffprobe)
            except (_Tool, ValueError):
                out.reason = "reference_unreadable"
                return out
            try:
                info_a = probe(pa, ffprobe)
            except (_Tool, ValueError):
                out.status, out.reason = FAILED, "primary_not_a_valid_video"
                return out
        except OSError:
            out.reason = "unreadable_file"
            return out
        out.scores["primary"], out.scores["reference"] = info_a, info_b
        if info_b.get("video_streams") != 1 or not info_b.get("width"):
            out.reason = "reference_unreadable"
            return out

        fail: list[str] = []
        if info_a.get("video_streams") != 1:
            fail.append("primary_video_stream_count")
        if info_a.get("audio_streams") != info_b.get("audio_streams"):
            fail.append("audio_stream_count")
        da, db_ = info_a.get("duration"), info_b.get("duration")
        if da is None or db_ is None or abs(da - db_) > th["max_duration_diff_s"]:
            fail.append("duration")
        fa, fb = info_a.get("frames"), info_b.get("frames")
        if fa is not None and fb is not None and abs(fa - fb) > th["max_frame_count_diff"]:
            fail.append("frame_count")
        if (info_a.get("width"), info_a.get("height")) != (info_b.get("width"), info_b.get("height")):
            fail.append("resolution")
        out.checks.append({"check": "stream_structure", "ok": not fail, "failed": list(fail)})
        if "primary_video_stream_count" in fail or "resolution" in fail:
            out.status, out.reason = FAILED, "stream_structure_mismatch"
            return out

        w = ANALYSIS_WIDTH if info_b["width"] > ANALYSIS_WIDTH else int(info_b["width"])
        h = max(2, int(round(w * info_b["height"] / info_b["width"] / 2.0)) * 2)
        w = max(2, w - (w % 2))
        n = int(th["max_frames_analysed"])
        try:
            frames_b = decode_gray_frames(pb, ffmpeg, w, h, fb, n)
        except (_Tool, ValueError):
            out.reason = "reference_unreadable"
            return out
        try:
            frames_a = decode_gray_frames(pa, ffmpeg, w, h, fa, n)
        except (_Tool, ValueError):
            out.status, out.reason = FAILED, "primary_not_decodable"
            return out
        if not frames_b:
            out.reason = "reference_unreadable"
            return out
        if len(frames_a) < len(frames_b):
            fail.append("decoded_fewer_frames")
        m = compare_frame_sequences(frames_a, frames_b, th)
        out.scores.update(m)
        quality_ok = (m["mean_psnr"] >= th["min_mean_psnr_db"] and m["min_psnr"] >= th["min_frame_psnr_db"]
                      and m["mean_ssim"] >= th["min_mean_ssim"])
        out.checks.append({"check": "psnr_ssim", "ok": quality_ok, **m})
        if not quality_ok:
            fail.append("quality")
        out.status = PASSED if not fail else FAILED
        out.reason = None if not fail else "encode_check_failed:" + ",".join(sorted(set(fail)))
        return out
