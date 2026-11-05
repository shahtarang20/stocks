"""U19: signed job progress reporter (POST /v1/telemetry/job/progress).

Same HMAC scheme as TelemetryCollector. Rate-limits itself client-side; the server
also enforces a minimum interval and drops reports for jobs not on this machine.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .heartbeat import sign_contract


class ProgressReporter:
    def __init__(
        self,
        machine_id: str,
        machine_secret: str,
        base_url: str = "http://localhost:8000",
        min_interval_seconds: float = 2.0,
    ) -> None:
        self.machine_id = machine_id
        self.machine_secret = machine_secret
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval_seconds
        self._last: dict[str, float] = {}

    def report(
        self,
        job_id: str,
        progress_pct: float,
        *,
        phase: str | None = None,
        gpu_util_pct: float | None = None,
        mem_used_mb: int | None = None,
        mem_total_mb: int | None = None,
        step_count: int | None = None,
        process_names: list[str] | None = None,
        blocked_egress: list[str] | None = None,
        power_w: float | None = None,
        io_bytes: int | None = None,
        transport: Any = None,
        timeout: float = 5.0,
    ) -> dict[str, Any] | None:
        """Send one progress point; returns None if throttled client-side. Never raises on 4xx/5xx."""
        now = time.monotonic()
        if progress_pct < 100.0 and now - self._last.get(job_id, -1e9) < self.min_interval:
            return None
        self._last[job_id] = now
        point = {
            "job_id": job_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "progress_pct": max(0.0, min(100.0, float(progress_pct))),
            "phase": (phase or None) and phase[:64],
            "gpu_util_pct": gpu_util_pct,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
            "step_count": step_count,
        }
        # Additive BYOC runtime signals (U16): omitted entirely unless sampled, so non-BYOC payloads are unchanged.
        if process_names:
            point["process_names"] = [str(n)[:64] for n in process_names[:64]]
        if blocked_egress:
            point["blocked_egress"] = [str(d)[:200] for d in blocked_egress[:50]]
        if power_w is not None:
            point["power_w"] = max(0.0, float(power_w))
        if io_bytes is not None:
            point["io_bytes"] = max(0, int(io_bytes))
        body = json.dumps({"points": [point]}, separators=(",", ":")).encode()
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "X-ComputeCoin-Machine": self.machine_id,
            "X-ComputeCoin-Timestamp": str(ts),
            "X-ComputeCoin-Signature": sign_contract(self.machine_id, ts, body, self.machine_secret),
        }
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        try:
            with httpx.Client(**kwargs) as c:
                r = c.post(f"{self.base_url}/v1/telemetry/job/progress", content=body, headers=headers)
                return r.json() if r.status_code == 200 else None
        except Exception:
            return None  # progress is best-effort; never fail the job over it
