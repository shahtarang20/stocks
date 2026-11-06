"""Session snapshots: how the supervisor learns what only the user's desktop session can see.

A Windows Service runs in session 0. ``GetLastInputInfo`` (idle time), ``GetForegroundWindow`` and
``SHQueryUserNotificationState`` (full-screen) describe the INTERACTIVE session, so the service cannot call them
itself. A small helper process in the user's session (``python -m computecoin_agent.windows.governance.probes
--snapshot-json``) calls them and reports one JSON object per interval; the supervisor feeds those objects to
:class:`SnapshotProbe`, which implements the PAL ``ActivityProbe`` and the owner-GPU probe for the governance engine.

Fail closed (L1): a snapshot older than ``max_age_seconds`` is an error, not "the owner is away". The one exception is
when the supervisor knows no user is logged in (``no_session()`` is True): then nobody is using the machine.

Schema (version 1), all keys required, ``null`` = unknown::

    {"v": 1, "taken_at": <unix seconds float>, "fullscreen": bool|null, "idle_seconds": float|null,
     "foreground_gpu_app": str|null, "free_vram_mb": int|null}
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable, Optional

from ...platform import ActivityProbe, ActivitySnapshot
from .policy import ProbeUnsupported

SCHEMA_VERSION = 1
_AWAY_IDLE_SECONDS = 10.0**9  # "no one has touched anything for a very long time"


class SnapshotError(RuntimeError):
    """The helper's report is missing, stale or malformed."""


def encode_snapshot(
    *,
    taken_at: float,
    fullscreen: Optional[bool],
    idle_seconds: Optional[float],
    foreground_gpu_app: Optional[str],
    free_vram_mb: Optional[int],
) -> str:
    return json.dumps(
        {
            "v": SCHEMA_VERSION,
            "taken_at": taken_at,
            "fullscreen": fullscreen,
            "idle_seconds": idle_seconds,
            "foreground_gpu_app": foreground_gpu_app,
            "free_vram_mb": free_vram_mb,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_snapshot(raw: Any) -> dict:
    """Strict parse of one helper line; raises :class:`SnapshotError` on anything unexpected."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else dict(raw)
    except (ValueError, TypeError):
        raise SnapshotError("snapshot is not valid JSON")
    if not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION:
        raise SnapshotError("unsupported snapshot version")
    expected = {"v", "taken_at", "fullscreen", "idle_seconds", "foreground_gpu_app", "free_vram_mb"}
    if set(data) != expected:
        raise SnapshotError("snapshot has missing or unknown fields")
    taken = data["taken_at"]
    if isinstance(taken, bool) or not isinstance(taken, (int, float)) or not math.isfinite(taken):
        raise SnapshotError("bad taken_at")
    if data["fullscreen"] is not None and not isinstance(data["fullscreen"], bool):
        raise SnapshotError("bad fullscreen")
    idle = data["idle_seconds"]
    if idle is not None and (isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle < 0):
        raise SnapshotError("bad idle_seconds")
    app = data["foreground_gpu_app"]
    if app is not None and (not isinstance(app, str) or len(app) > 260):
        raise SnapshotError("bad foreground_gpu_app")
    free = data["free_vram_mb"]
    if free is not None and (isinstance(free, bool) or not isinstance(free, int) or free < 0):
        raise SnapshotError("bad free_vram_mb")
    return data


class SnapshotProbe(ActivityProbe):
    """PAL ``ActivityProbe`` plus the owner-GPU methods, backed by the helper's latest report.

    ``source()`` returns the latest raw report (a JSON string or dict) or None when the helper has said nothing yet.
    """

    platform_name = "windows"

    def __init__(
        self,
        source: Callable[[], Any],
        *,
        max_age_seconds: float = 30.0,
        no_session: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._max_age = max_age_seconds
        self._no_session = no_session
        self._clock = clock

    def _latest(self) -> Optional[dict]:
        """The fresh report, or None when no user is logged in. Raises SnapshotError when it should exist but does not."""
        if self._no_session is not None and self._no_session():
            return None
        raw = self._source()
        if raw is None:
            raise SnapshotError("the session helper has not reported")
        data = decode_snapshot(raw)
        age = self._clock() - data["taken_at"]
        if age > self._max_age or age < -5.0:  # a report from the future is as untrustworthy as a stale one
            raise SnapshotError("the session helper's report is stale")
        return data

    def snapshot(self) -> ActivitySnapshot:
        data = self._latest()
        if data is None:  # nobody is logged in
            return ActivitySnapshot(owner_active=False, fullscreen_app=False, idle_seconds=_AWAY_IDLE_SECONDS)
        idle = data["idle_seconds"]
        return ActivitySnapshot(owner_active=None, fullscreen_app=data["fullscreen"], idle_seconds=idle)

    # owner-GPU probe (same duck type as ``WindowsOwnerGpuProbe``)
    def foreground_gpu_app(self) -> Optional[str]:
        data = self._latest()
        return None if data is None else data["foreground_gpu_app"]

    def free_vram_mb(self) -> Optional[int]:
        data = self._latest()
        if data is None:
            raise ProbeUnsupported("no session: VRAM is read by the helper")
        return data["free_vram_mb"]
