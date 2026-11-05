"""Deterministic smoke fixture for batch-embeddings: python make_fixture.py texts.jsonl"""

import json
import sys

words = ["river", "stone", "cloud", "engine", "garden", "signal", "window", "forest", "bridge", "planet"]
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    for n in range(100):
        text = " ".join(words[(n * k + k) % len(words)] for k in range(1, 7)) + f" number {n}."
        fh.write(json.dumps({"id": f"t{n:03d}", "text": text}) + "\n")
