"""Log-mel spectrogram similarity between the host's WAV files and an independent re-run (text-to-speech kits).

Files are paired by basename (``*.wav`` only). Each file is read with the standard-library ``wave`` module (PCM 16-bit or
32-bit integer, any channel count, mixed to mono), turned into a log-mel spectrogram with numpy only (Hann window, FFT size
1024, hop 256, 64 HTK mel bands, absolute floor at -60 dB power so silence and 16-bit quantisation noise map to 0), and the two spectrograms are compared by cosine
similarity of the flattened matrices. Durations must agree within ``max_duration_diff_s``.

Outcomes: a missing or unreadable reference is ``inconclusive`` (our problem); a missing, unreadable, empty or
sample-rate-mismatched primary is ``failed``. Thresholds are conservative defaults and an OWNER DECISION per kit.
Written, never run. Library behaviour to verify against current docs (L4): ``wave.open`` / ``readframes``,
``numpy.fft.rfft``, ``numpy.lib.stride_tricks.sliding_window_view``.
"""

from __future__ import annotations

import wave
from pathlib import Path

from .base import FAILED, INCONCLUSIVE, PASSED, ThresholdError, VerifyOutcome, resolve_thresholds

MAX_FILE_BYTES = 512 * 1024**2
MAX_SECONDS = 900.0
MAX_SAMPLES = 64_000_000  # decoded samples (frames x channels) per file: the float64 decode is 8 bytes each (512 MB)
MAX_FILES = 256
N_FFT = 1024
HOP = 256
N_MELS = 64
FRAME_BLOCK = 2048  # spectrogram frames processed at a time, to bound memory


class _Unreadable(Exception):
    pass


def read_wav(path: Path):
    """Return (mono float64 samples in [-1, 1], sample_rate). Raises _Unreadable."""
    import numpy as np

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise _Unreadable("file_too_large")
        with wave.open(str(path), "rb") as w:
            rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
            if rate <= 0 or ch < 1 or n <= 0:
                raise _Unreadable("empty_audio")
            if n / rate > MAX_SECONDS or n * ch > MAX_SAMPLES:
                raise _Unreadable("audio_too_long")
            raw = w.readframes(n)
    except _Unreadable:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise _Unreadable(f"unreadable_wav:{type(exc).__name__}")
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise _Unreadable("unsupported_sample_width")
    if x.size % ch:
        raise _Unreadable("truncated_frames")
    return x.reshape(-1, ch).mean(axis=1), rate


def mel_filterbank(rate: int, n_fft: int = N_FFT, n_mels: int = N_MELS):
    """HTK mel triangular filters, shape (n_mels, n_fft // 2 + 1). Pure numpy."""
    import numpy as np

    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    freqs = np.linspace(0.0, rate / 2.0, n_fft // 2 + 1)
    pts = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(rate / 2.0), n_mels + 2))
    fb = np.zeros((n_mels, freqs.size))
    for i in range(n_mels):
        lo, mid, hi = pts[i], pts[i + 1], pts[i + 2]
        up = (freqs - lo) / max(mid - lo, 1e-9)
        down = (hi - freqs) / max(hi - mid, 1e-9)
        fb[i] = np.maximum(0.0, np.minimum(up, down))
    return fb


def log_mel_blocks(x, rate: int):
    """Yield (frames, n_mels) log-mel blocks. Value = max(log10(mel power + 1e-12) + 6, 0): an absolute floor at 1e-6 power, so silence and 16-bit
    quantisation noise are 0 and only audible content contributes to the similarity."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    if x.size < N_FFT:
        x = np.pad(x, (0, N_FFT - x.size))
    fb = mel_filterbank(rate)
    win = np.hanning(N_FFT)
    frames = sliding_window_view(x, N_FFT)[::HOP]
    for i in range(0, frames.shape[0], FRAME_BLOCK):
        block = frames[i:i + FRAME_BLOCK] * win
        power = np.abs(np.fft.rfft(block, axis=1)) ** 2
        mel = power @ fb.T
        yield np.maximum(np.log10(mel + 1e-12) + 6.0, 0.0)


def spectral_cosine(a, rate_a: int, b, rate_b: int) -> float:
    """Cosine similarity of the flattened log-mel spectrograms over the common number of frames. Two silent files: 1.0."""
    import numpy as np

    n = min(((a.size - N_FFT) // HOP + 1) if a.size >= N_FFT else 1, ((b.size - N_FFT) // HOP + 1) if b.size >= N_FFT else 1)
    dot = na = nb = 0.0
    seen = 0
    for ba, bb in zip(log_mel_blocks(a, rate_a), log_mel_blocks(b, rate_b)):
        take = min(ba.shape[0], bb.shape[0], n - seen)
        if take <= 0:
            break
        ba, bb = ba[:take], bb[:take]
        dot += float(np.sum(ba * bb))
        na += float(np.sum(ba * ba))
        nb += float(np.sum(bb * bb))
        seen += take
    if na == 0.0 and nb == 0.0:
        return 1.0
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / ((na ** 0.5) * (nb ** 0.5))))


class AudioSpectralPlugin:
    method = "audio_spectral"
    default_thresholds: dict[str, float] = {"min_cosine": 0.90, "max_duration_diff_s": 0.25}  # OWNER DECISION
    threshold_bounds: dict[str, tuple[float, float]] = {"min_cosine": (0.5, 1.0), "max_duration_diff_s": (0.0, 5.0)}

    def verify(self, primary, reference, thresholds=None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE)
        try:
            th = resolve_thresholds(self.default_thresholds, self.threshold_bounds, thresholds or None)
        except ThresholdError as exc:
            out.reason = f"invalid_thresholds:{exc}"
            return out
        out.thresholds = th
        try:
            import numpy  # noqa: F401
        except ImportError:
            out.reason = "numpy_unavailable"
            return out
        ref = {p.name: p for p in reference if p.name.lower().endswith(".wav")}
        pri = {p.name: p for p in primary if p.name.lower().endswith(".wav")}
        if not ref:
            out.reason = "reference_missing"
            return out
        if not pri:
            out.status, out.reason = FAILED, "primary_output_missing"
            return out
        if len(ref) > MAX_FILES or len(pri) > MAX_FILES:
            out.reason = "too_many_files"
            return out
        if set(ref) != set(pri):
            out.status, out.reason = FAILED, "file_set_mismatch"
            out.checks.append({"check": "file_set", "ok": False, "unmatched": sorted(set(ref) ^ set(pri))[:20]})
            return out
        lowest, total, bad = 1.0, 0.0, []
        for name in sorted(ref):
            try:
                xr, rr = read_wav(ref[name])
            except _Unreadable:
                out.reason = "unreadable_reference"
                return out
            try:
                xp, rp = read_wav(pri[name])
            except _Unreadable as exc:
                out.status, out.reason = FAILED, "primary_unreadable"
                out.checks.append({"check": "readable", "ok": False, "file": name, "detail": str(exc)})
                return out
            if rp != rr:
                out.status, out.reason = FAILED, "sample_rate_mismatch"
                out.checks.append({"check": "sample_rate", "ok": False, "file": name, "primary": rp, "reference": rr})
                return out
            if abs(xp.size / rp - xr.size / rr) > th["max_duration_diff_s"]:
                bad.append(name)
                continue
            sim = spectral_cosine(xp, rp, xr, rr)
            lowest = min(lowest, sim)
            total += sim
            if sim < th["min_cosine"]:
                bad.append(name)
        out.scores = {"files": len(ref), "min_cosine": round(lowest, 6), "mean_cosine": round(total / len(ref), 6),
                      "files_below": len(bad)}
        out.checks.append({"check": "every_file_similar_and_same_duration", "ok": not bad, "files": bad[:20]})
        out.status = PASSED if not bad else FAILED
        out.reason = None if not bad else "spectral_mismatch"
        return out
