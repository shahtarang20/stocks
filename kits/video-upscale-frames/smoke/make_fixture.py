"""Deterministic smoke fixture for video-upscale-frames: python make_fixture.py frames.zip (four 64x48 PNG frames)."""

import io
import sys
import zipfile

import numpy as np
from PIL import Image

with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_STORED) as zf:
    for n in range(1, 5):
        y, x = np.mgrid[0:48, 0:64]
        img = np.stack([x * 4, y * 5, (x + y + n * 10)], axis=2).astype(np.int64)
        img[10:30, 8 + n * 6:28 + n * 6] = 220
        buf = io.BytesIO()
        Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(buf, format="PNG")
        zf.writestr(zipfile.ZipInfo(f"{n:04d}.png", date_time=(2020, 1, 1, 0, 0, 0)), buf.getvalue())
