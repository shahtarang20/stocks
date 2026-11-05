"""Example BYOC entrypoint: reads /in, writes a result file under /out. Replace with your program."""

import json
import os
import sys

IN_DIR = os.environ.get("COMPUTECOIN_INPUTS_DIR", "/in")
OUT_DIR = os.environ.get("COMPUTECOIN_OUT_DIR", "/out")


def main() -> int:
    names = sorted(os.listdir(IN_DIR)) if os.path.isdir(IN_DIR) else []
    os.makedirs(os.path.join(OUT_DIR, "result"), exist_ok=True)
    with open(os.path.join(OUT_DIR, "result", "out.json"), "w", encoding="utf-8") as fh:
        json.dump({"inputs": names, "app_mode": os.environ.get("APP_MODE", "default")}, fh, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
