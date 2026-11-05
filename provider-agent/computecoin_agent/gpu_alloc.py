"""Multi-GPU device sets on one host (U13, docs/architecture/U13-multi-gpu-design.md). Written, never run.

The agent, not the container, decides which GPUs a job sees: a job asking for N GPUs gets the N lowest free indices,
held until the job ends, so two jobs never share a card. ADR-0003 limits are kept: private IPC (never --ipc=host),
a bounded --shm-size, no extra capabilities.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

MAX_SHM_MB = 16384
MIN_SHM_MB = 64


class GpuAllocationError(RuntimeError):
    pass


class GpuAllocator:
    def __init__(self, total: int) -> None:
        self._total = max(0, int(total))
        self._in_use: set[int] = set()
        self._lock = threading.Lock()

    def acquire(self, count: int) -> list[int]:
        with self._lock:
            free = [i for i in range(self._total) if i not in self._in_use]
            if count < 1 or len(free) < count:
                raise GpuAllocationError(f"need {count} free GPU(s), {len(free)} free of {self._total}")
            chosen = free[:count]
            self._in_use.update(chosen)
            return chosen

    def release(self, ids: list[int]) -> None:
        with self._lock:
            self._in_use.difference_update(ids)


def nvidia_gpus_args(ids: list[int]) -> list[str]:
    """Docker argv for an explicit device set. The literal inner quotes are required by docker's --gpus parser when the
    value holds a comma (VERIFY against current Docker/NVIDIA Container Toolkit docs, L4)."""
    if not ids or any((not isinstance(i, int)) or i < 0 for i in ids):
        raise GpuAllocationError("invalid device ids")
    return ["--gpus", '"device=' + ",".join(str(i) for i in sorted(set(ids))) + '"']


def shm_size_mb(memory_mb: int, limits: Optional[dict[str, Any]] = None) -> int:
    """Shared memory for NCCL / data loaders: the job's ``shm_mb`` if given, else a quarter of its memory limit,
    always at most half the memory limit (shm counts against it) and at most MAX_SHM_MB."""
    want = int((limits or {}).get("shm_mb") or memory_mb // 4)
    return max(MIN_SHM_MB, min(want, memory_mb // 2, MAX_SHM_MB))


def nccl_env(gpu_count: int, rendezvous: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """Environment for a multi-GPU job. Single host: peer-to-peer and shared memory only, no network probing.
    Multi-node adds the standard torchrun variables from the claim's rendezvous block."""
    env = {"NCCL_DEBUG": "WARN", "COMPUTECOIN_GPU_COUNT": str(gpu_count)}
    if rendezvous is None:
        env.update({"NCCL_IB_DISABLE": "1", "NCCL_SOCKET_IFNAME": "lo"})
        return env
    for key in ("master_addr", "master_port", "node_rank", "nnodes", "gpus_per_node", "world_size"):
        if key not in rendezvous:
            raise GpuAllocationError(f"rendezvous missing {key}")
    env.update({
        "MASTER_ADDR": str(rendezvous["master_addr"]), "MASTER_PORT": str(int(rendezvous["master_port"])),
        "NODE_RANK": str(int(rendezvous["node_rank"])), "NNODES": str(int(rendezvous["nnodes"])),
        "NPROC_PER_NODE": str(int(rendezvous["gpus_per_node"])), "WORLD_SIZE": str(int(rendezvous["world_size"])),
    })
    return env
