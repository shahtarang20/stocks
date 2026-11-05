"""Deterministic smoke fixture for llm-finetune-lora: python make_fixture.py dataset.jsonl (64 tiny rows)."""

import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as fh:
    for n in range(64):
        fh.write(json.dumps({"prompt": f"Q: what is {n} plus {n}? A:", "completion": f" {2 * n}"}) + "\n")
