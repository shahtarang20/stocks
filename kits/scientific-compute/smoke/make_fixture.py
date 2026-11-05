"""Deterministic smoke fixture for scientific-compute: python make_fixture.py script.py (a small numpy/scipy/pandas job)."""

import sys

SCRIPT = '''\
import json, os
import numpy as np
import pandas as pd
from scipy import linalg

seed = int(os.environ["KIT_SEED"])
rng = np.random.default_rng(seed)
a = rng.standard_normal((64, 64))
a = a @ a.T + 64 * np.eye(64)
w = linalg.eigvalsh(a)
df = pd.DataFrame({"index": np.arange(8), "eigenvalue": np.round(w[:8], 9)})
df.to_csv("table.csv", index=False)
with open("summary.json", "w") as fh:
    json.dump({"trace": round(float(np.trace(a)), 9), "min_eig": round(float(w[0]), 9)}, fh, sort_keys=True)
print("done")
'''

with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(SCRIPT)
