"""BYOC plan parsing on the agent (U8, docs/architecture/U8-byoc-design.md).

The API validates the spec, but the agent never trusts the claim: it re-checks every field and refuses anything odd.
There is no code path here that can produce privileged mode, host mounts, devices, capabilities, host namespaces or
the Docker socket.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,126}(:[0-9]{1,5})?(/[a-z0-9][a-z0-9._\-]{0,127}){1,8}@sha256:[0-9a-f]{64}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV_PREFIXES = ("COMPUTECOIN_", "LD_", "DOCKER_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")
TIER_OF_ISOLATION = {"gvisor": "gvisor", "microvm": "microvm"}
MAX_ITEMS, MAX_ITEM_CHARS, MAX_TOTAL = 32, 512, 8192
MAX_MEMORY_MB, MAX_GPUS, MAX_PIDS = 262144, 8, 4096


class ByocError(ValueError):
    pass


@dataclass
class ByocPlan:
    image: str
    tier: str  # gvisor | microvm (never container)
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    gpu_count: int = 1
    memory_mb: int = 2048
    cpus: str = "1"
    pids_limit: int = 256
    max_runtime_seconds: Optional[int] = None


def _argv(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ByocError(f"bad {what}")
    total = 0
    for it in value:
        if not isinstance(it, str) or len(it) > MAX_ITEM_CHARS or "\x00" in it:
            raise ByocError(f"bad {what}")
        total += len(it)
    if total > MAX_TOTAL:
        raise ByocError(f"{what} too long")
    return list(value)


def parse_byoc(v2: Optional[dict[str, Any]]) -> Optional[ByocPlan]:
    """None when the claim is not a BYOC job. Raises ByocError for a malformed or unsafe block (job fails)."""
    block = v2.get("byoc") if isinstance(v2, dict) else None
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ByocError("bad byoc block")
    image = block.get("image")
    if not isinstance(image, str) or not IMAGE_RE.match(image):
        raise ByocError("byoc image must be pinned by sha256 digest")
    tier = TIER_OF_ISOLATION.get(str(block.get("isolation")))
    if tier is None:
        raise ByocError("byoc isolation must be gvisor or microvm")
    env = block.get("env") or {}
    if not isinstance(env, dict) or len(env) > MAX_ITEMS:
        raise ByocError("bad env")
    for k, val in env.items():
        if (not isinstance(k, str) or not ENV_RE.match(k) or any(k.startswith(p) for p in RESERVED_ENV_PREFIXES)
                or not isinstance(val, str) or len(val) > 1024 or any(c in val for c in "\x00\n\r")):
            raise ByocError("bad env entry")
    try:
        gpu = int(block.get("gpu_count", 1))
        mem = int(block.get("memory_mb", 2048))
        pids = int(block.get("pids_limit", 256))
        cpus = float(block.get("cpus", "1"))
        runtime = block.get("max_runtime_seconds")
        runtime = int(runtime) if runtime is not None else None
    except (TypeError, ValueError):
        raise ByocError("bad limits")
    if not (0 <= gpu <= MAX_GPUS and 64 <= mem <= MAX_MEMORY_MB and 16 <= pids <= MAX_PIDS and 0.1 <= cpus <= 256):
        raise ByocError("limits out of range")
    if runtime is not None and runtime < 1:
        raise ByocError("bad limits")
    return ByocPlan(image=image, tier=tier, command=_argv(block.get("command"), "command"),
                    args=_argv(block.get("args"), "args"), env=dict(env), gpu_count=gpu, memory_mb=mem,
                    cpus=f"{cpus:g}", pids_limit=pids, max_runtime_seconds=runtime)
