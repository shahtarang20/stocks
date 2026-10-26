"""Freivalds' algorithm: check a claimed matrix product C = A x B far more cheaply than redoing it.

Each round draws r in {-1, +1}^p and compares A(Br) with Cr: O(mn + np) work instead of O(mnp).
If C is wrong, one round misses it with probability at most 1/2, so ``rounds`` rounds miss it with
probability at most 2^-rounds. The random vector comes from the OS CSPRNG so a host cannot predict
it and craft a C that passes.
"""

from __future__ import annotations

import math
import secrets
from typing import Sequence

MAX_DIM = 512  # bounds the request cost; larger checks belong in a worker, not a web request

_rng = secrets.SystemRandom()


def _shape(name: str, m: Sequence[Sequence[float]]) -> tuple[int, int]:
    if not m or not m[0]:
        raise ValueError(f"{name} is empty")
    cols = len(m[0])
    if len(m) > MAX_DIM or cols > MAX_DIM:
        raise ValueError(f"{name} is larger than {MAX_DIM}x{MAX_DIM}")
    for row in m:
        if len(row) != cols:
            raise ValueError(f"{name} is not rectangular")
        for v in row:
            if not math.isfinite(v):
                raise ValueError(f"{name} contains NaN or infinity")
    return len(m), cols


def _matvec(M: Sequence[Sequence[float]], v: Sequence[float]) -> list[float]:
    return [math.fsum(a * b for a, b in zip(row, v)) for row in M]


def freivalds_verify(
    A: Sequence[Sequence[float]],
    B: Sequence[Sequence[float]],
    C: Sequence[Sequence[float]],
    *,
    rounds: int = 10,
    tolerance: float = 1e-6,
) -> bool:
    m, n = _shape("A", A)
    nb, p = _shape("B", B)
    mc, pc = _shape("C", C)
    if nb != n:
        raise ValueError(f"A is {m}x{n} but B has {nb} rows")
    if (mc, pc) != (m, p):
        raise ValueError(f"C is {mc}x{pc}, expected {m}x{p}")
    if not 1 <= rounds <= 64:
        raise ValueError("rounds must be between 1 and 64")

    for _ in range(rounds):
        r = [1.0 if _rng.getrandbits(1) else -1.0 for _ in range(p)]
        left = _matvec(A, _matvec(B, r))
        right = _matvec(C, r)
        for a, c in zip(left, right):
            # Relative to the magnitude of the row sums, so large values are not judged by an absolute 1e-6.
            if not math.isclose(a, c, rel_tol=tolerance, abs_tol=tolerance):
                return False
    return True
