"""Is this process running inside WSL (a Linux system on Windows)? Used where Docker behaves differently there.

NVIDIA's CUDA-on-WSL guide (read 2026-11-17): in WSL 2 Docker supports ONLY ``--gpus all``, so a GPU cannot be chosen by count or by index.
WSL gives the Linux system one GPU at a time, so ``all`` is exactly one card.

Detection never raises. The kernel string of a WSL 2 system contains both "microsoft" and "WSL" (for example ``5.15.153.1-microsoft-standard-WSL2``),
and WSL sets ``WSL_DISTRO_NAME`` for every program it starts.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional


def running_in_wsl(env: Optional[Mapping[str, str]] = None, proc_version: Optional[str] = None) -> bool:
    env = os.environ if env is None else env
    if (env.get("WSL_DISTRO_NAME") or "").strip():
        return True
    if proc_version is None:
        try:
            with open("/proc/version", encoding="utf-8", errors="replace") as fh:
                proc_version = fh.read(512)
        except OSError:
            return False
    low = proc_version.lower()
    return "microsoft" in low and "wsl" in low
