"""Perceptual image/frame check (renders, generated images, upscales).

Catches an altered frame even when it passes a coarse hash: a localized edit is caught by the block
difference, a global change by the perceptual hash and SSIM. Honest re-renders on a different GPU
differ by noise only, so every statistic is averaged (hash on a downscaled image, mean over blocks).

Library calls to verify against current docs before merge (L4): Pillow Image.open(formats=...),
imagehash.phash(hash_size=), skimage.metrics.structural_similarity(data_range=, ...).
Thresholds below are conservative defaults and an OWNER DECISION per kit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import (FAILED, INCONCLUSIVE, PASSED, ThresholdError, VerifyOutcome, pair_by_name,
                   resolve_thresholds)

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 100_000_000
ALLOWED_FORMATS = ["PNG", "JPEG", "WEBP", "TIFF", "BMP"]
BLOCKS = 16  # blocks per side for the localized-difference check


class _Unreadable(Exception):
    pass


def _load_rgb(path: Path):
    """Load an untrusted image as an RGB numpy array (uint8). Raises _Unreadable."""
    import numpy as np
    from PIL import Image

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise _Unreadable("file_too_large")
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with Image.open(path, formats=ALLOWED_FORMATS) as img:
            if img.width * img.height > MAX_PIXELS:
                raise _Unreadable("too_many_pixels")
            img.load()
            return np.asarray(img.convert("RGB"), dtype=np.uint8), img.copy()
    except _Unreadable:
        raise
    except Exception as exc:  # Pillow raises many types, incl. DecompressionBombError
        raise _Unreadable(f"unreadable_image:{type(exc).__name__}")


def diff_stats(a, b, blocks: int = BLOCKS) -> tuple[float, float]:
    """(mean absolute difference, largest block mean difference), both 0..1, of two equal-shape uint8 arrays.

    Works one block-row band at a time: the whole-image int16 temporaries of the naive form cost ~8 bytes per pixel
    several times over, which a 200 MP host image turned into a multi-GB allocation on the verification worker."""
    import numpy as np

    h, w = a.shape[:2]
    ys = np.linspace(0, h, min(blocks, h) + 1, dtype=int)
    xs = np.linspace(0, w, min(blocks, w) + 1, dtype=int)
    channels = a.shape[2] if a.ndim == 3 else 1
    total, worst = 0.0, 0.0
    for i in range(len(ys) - 1):
        ab, bb = a[ys[i]:ys[i + 1]], b[ys[i]:ys[i + 1]]
        band = np.maximum(ab, bb) - np.minimum(ab, bb)  # uint8 |a-b| without widening
        total += float(band.sum(dtype=np.float64))
        for j in range(len(xs) - 1):
            cell = band[:, xs[j]:xs[j + 1]]
            if cell.size:
                worst = max(worst, float(cell.sum(dtype=np.float64)) / (cell.size) / 255.0)
    return total / (h * w * channels) / 255.0, worst


def block_diff_max(a, b, blocks: int = BLOCKS) -> float:
    """Largest mean absolute difference (0..1) over a blocks x blocks grid of two equal-size arrays."""
    return diff_stats(a, b, blocks)[1]


def compare_images(a_arr, a_img, b_arr, b_img) -> dict[str, float]:
    import imagehash
    import numpy as np
    from PIL import Image
    from skimage.metrics import structural_similarity

    hash_bits = 16 * 16
    dist = (imagehash.phash(a_img, hash_size=16) - imagehash.phash(b_img, hash_size=16)) / hash_bits
    mad, blk = diff_stats(a_arr, b_arr)

    def small_gray(img):
        g = img.convert("L")
        g.thumbnail((1024, 1024), Image.LANCZOS)
        return np.asarray(g, dtype=np.uint8)

    ga, gb = small_gray(a_img), small_gray(b_img)
    ssim = float(structural_similarity(ga, gb, data_range=255)) if min(ga.shape) >= 7 else 1.0
    return {"phash_distance": round(float(dist), 5), "mean_abs_diff": round(mad, 5),
            "max_block_diff": round(blk, 5), "ssim": round(ssim, 5)}


class PerceptualPlugin:
    method = "perceptual"
    # OWNER DECISION: conservative starting points, to be tuned from honest cross-GPU benchmark runs (U5.3).
    default_thresholds: dict[str, float] = {
        "max_phash_distance": 0.12,   # fraction of 256 hash bits that may differ
        "max_mean_abs_diff": 0.03,    # mean |a-b| / 255 over the whole image
        "max_block_diff": 0.12,       # worst 1/16 x 1/16 block; catches a localized edit
        "min_ssim": 0.85,             # grayscale, downscaled to <= 1024 px
        "max_failed_fraction": 0.0,   # share of sampled images allowed to fail (0 = none)
    }
    threshold_bounds: dict[str, tuple[float, float]] = {
        "max_phash_distance": (0.0, 0.5), "max_mean_abs_diff": (0.0, 0.2),
        "max_block_diff": (0.0, 0.5), "min_ssim": (0.3, 1.0), "max_failed_fraction": (0.0, 0.5),
    }

    def verify(self, primary: list[Path], reference: list[Path], thresholds: dict[str, float] | None = None) -> VerifyOutcome:
        out = VerifyOutcome(method=self.method, status=INCONCLUSIVE)
        try:
            th = resolve_thresholds(self.default_thresholds, self.threshold_bounds, thresholds)
        except ThresholdError as exc:
            out.reason = f"invalid_thresholds:{exc}"
            return out
        out.thresholds = th
        try:
            import imagehash  # noqa: F401
            import numpy  # noqa: F401
            import PIL  # noqa: F401
            import skimage  # noqa: F401
        except ImportError:
            out.reason = "verification_dependencies_missing"
            return out
        if not reference:
            out.reason = "reference_missing"
            return out
        pairs, unmatched = pair_by_name(primary, reference)
        if not pairs:
            out.status = FAILED if primary else INCONCLUSIVE
            out.reason = "no_comparable_files" if primary else "primary_output_missing"
            return out
        # A sampled reference covers only some names; the primary may hold more. The reverse (reference
        # holds a file the primary lacks) means the host dropped an output.
        missing = sorted({r.name for r in reference} - {p.name for p in primary})
        if missing:
            out.status, out.reason = FAILED, "primary_file_missing"
            out.checks.append({"check": "files_present", "ok": False, "missing": missing[:20]})
            return out

        failures: list[dict[str, Any]] = []
        worst: dict[str, float] = {}
        for pa, pb in pairs:
            try:
                a_arr, a_img = _load_rgb(pa)
                b_arr, b_img = _load_rgb(pb)
            except _Unreadable as exc:
                # An unreadable PRIMARY is the host's output being bad; an unreadable reference is ours.
                try:
                    _load_rgb(pb)
                except _Unreadable:
                    out.reason = f"reference_unreadable:{exc}"
                    return out
                failures.append({"file": pa.name, "reason": str(exc)})
                continue
            if a_arr.shape != b_arr.shape:
                failures.append({"file": pa.name, "reason": "size_mismatch",
                                 "primary": list(a_arr.shape[:2]), "reference": list(b_arr.shape[:2])})
                continue
            m = compare_images(a_arr, a_img, b_arr, b_img)
            for k in ("phash_distance", "mean_abs_diff", "max_block_diff"):
                worst[k] = max(worst.get(k, 0.0), m[k])
            worst["ssim"] = min(worst.get("ssim", 1.0), m["ssim"])
            bad = [name for name, ok in (
                ("phash", m["phash_distance"] <= th["max_phash_distance"]),
                ("mean_abs_diff", m["mean_abs_diff"] <= th["max_mean_abs_diff"]),
                ("block_diff", m["max_block_diff"] <= th["max_block_diff"]),
                ("ssim", m["ssim"] >= th["min_ssim"])) if not ok]
            if bad:
                failures.append({"file": pa.name, "failed": bad, **m})
        out.scores = {"compared": len(pairs), "failed": len(failures), "worst": worst}
        frac = len(failures) / len(pairs)
        ok = frac <= th["max_failed_fraction"]
        out.checks.append({"check": "perceptual", "ok": ok, "failures": failures[:20]})
        out.status = PASSED if ok else FAILED
        out.reason = None if ok else "perceptual_difference_over_threshold"
        return out
