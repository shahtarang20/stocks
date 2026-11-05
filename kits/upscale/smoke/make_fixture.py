"""Deterministic smoke fixture for the upscale Kit: python make_fixture.py out.zip"""

import sys
import zipfile
import io

import numpy as np
from PIL import Image

out = sys.argv[1]
with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
    for name, seed in (("a.png", 1), ("b.png", 2)):
        rng = np.random.default_rng(seed)
        y, x = np.mgrid[0:96, 0:128]
        img = np.stack([x * 2, y * 2, (x + y)], axis=2).astype(np.float64)
        img[20:60, 30:90] = 200 - seed * 40
        img = np.clip(img + rng.normal(0, 2, img.shape), 0, 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(img).save(buf, format="PNG")
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
        zf.writestr(info, buf.getvalue())
