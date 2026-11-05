"""Deterministic smoke fixture for ai-video image-to-video builds: python make_fixture.py image.png (256x256 gradient PNG)."""

import sys

import numpy as np
from PIL import Image

y, x = np.mgrid[0:256, 0:256]
img = np.stack([x, y, (x + y) // 2], axis=2).astype(np.uint8)
img[96:160, 96:160] = 230
Image.fromarray(img).save(sys.argv[1], format="PNG", optimize=False)
