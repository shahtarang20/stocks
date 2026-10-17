"""Phase 9: Provider Agent high-frequency telemetry collector and batch reporter.

Samples GPU utilization, VRAM usage, core temperatures, and power draw
periodically and flushes cryptographically signed batches to the control plane.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

import httpx

from .heartbeat import contract_canonical_string, sign_contract

logger = logging.getLogger(__name__)


@dataclass
class TelemetrySample:
    gpu_index: int = 0
    gpu_util_pct: float | None = None
    mem_util_pct: float | None = None
    mem_used_mb: int | None = None
    mem_total_mb: int | None = None
    temp_c: float | None = None
    power_w: float | None = None
    fan_speed_pct: float | None = None
    timestamp: float | None = None
    extra_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.timestamp is not None:
            # Format as ISO 8601 UTC timestamp or unix epoch
            d["timestamp"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)
            )
        else:
            d["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return d


def sample_gpu_metrics(
    nvml_module: Any = None,
    simulated: bool = False,
) -> list[TelemetrySample]:
    """Sample current metrics from all available NVIDIA GPUs or simulated fallback."""
    samples: list[TelemetrySample] = []
    now = time.time()

    if simulated or nvml_module is None:
        try:
            import pynvml
            nvml_module = pynvml
            nvml_module.nvmlInit()
        except Exception:
            nvml_module = None

    if nvml_module is not None:
        try:
            device_count = nvml_module.nvmlDeviceGetCount()
            for idx in range(device_count):
                handle = nvml_module.nvmlDeviceGetHandleByIndex(idx)
                util = nvml_module.nvmlDeviceGetUtilizationRates(handle)
                mem = nvml_module.nvmlDeviceGetMemoryInfo(handle)
                temp = nvml_module.nvmlDeviceGetTemperature(
                    handle, getattr(nvml_module, "NVML_TEMPERATURE_GPU", 0)
                )
                try:
                    power = nvml_module.nvmlDeviceGetPowerUsage(handle) / 1000.0
                except Exception:
                    power = None
                try:
                    fan = nvml_module.nvmlDeviceGetFanSpeed(handle)
                except Exception:
                    fan = None

                mem_total_mb = int(mem.total // (1024 * 1024))
                mem_used_mb = int(mem.used // (1024 * 1024))
                mem_util_pct = (
                    round((mem_used_mb / mem_total_mb) * 100.0, 1)
                    if mem_total_mb > 0
                    else 0.0
                )

                samples.append(
                    TelemetrySample(
                        gpu_index=idx,
                        gpu_util_pct=float(util.gpu),
                        mem_util_pct=mem_util_pct,
                        mem_used_mb=mem_used_mb,
                        mem_total_mb=mem_total_mb,
                        temp_c=float(temp),
                        power_w=float(power) if power is not None else None,
                        fan_speed_pct=float(fan) if fan is not None else None,
                        timestamp=now,
                    )
                )
            return samples
        except Exception as exc:
            logger.debug("Failed reading live NVML metrics: %s", exc)

    # Fallback simulated sample (CPU-only or offline dev environment)
    samples.append(
        TelemetrySample(
            gpu_index=0,
            gpu_util_pct=25.0,
            mem_util_pct=30.0,
            mem_used_mb=4096,
            mem_total_mb=16384,
            temp_c=48.5,
            power_w=85.0,
            fan_speed_pct=40.0,
            timestamp=now,
            extra_metrics={"mode": "simulated_probe"},
        )
    )
    return samples


class TelemetryCollector:
    """Sliding-window telemetry buffer and signed batch reporter."""

    def __init__(
        self,
        machine_id: str,
        machine_secret: str,
        base_url: str = "http://localhost:8000",
        max_buffer_size: int = 1000,
        sampler: Callable[[], list[TelemetrySample]] = sample_gpu_metrics,
    ) -> None:
        self.machine_id = machine_id
        self.machine_secret = machine_secret
        self.base_url = base_url.rstrip("/")
        self.max_buffer_size = max_buffer_size
        self.sampler = sampler
        self._buffer: collections.deque[TelemetrySample] = collections.deque(
            maxlen=max_buffer_size
        )

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    def record_sample(self, sample: TelemetrySample) -> None:
        """Add a single sample to the buffer."""
        self._buffer.append(sample)

    def sample_now(self) -> list[TelemetrySample]:
        """Trigger sampler and buffer the captured points."""
        samples = self.sampler()
        for s in samples:
            self._buffer.append(s)
        return samples

    def flush(
        self,
        transport: Any = None,
        timeout: float = 10.0,
        max_batch_size: int = 500,
    ) -> dict[str, Any]:
        """Send all buffered telemetry points to POST /v1/telemetry/machine."""
        if not self._buffer:
            return {"accepted": 0, "alerts_triggered": 0}

        # Drain up to max_batch_size points
        to_send: list[TelemetrySample] = []
        while self._buffer and len(to_send) < max_batch_size:
            to_send.append(self._buffer.popleft())

        payload = {"points": [pt.to_dict() for pt in to_send]}
        raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ts = int(time.time())
        sig = sign_contract(self.machine_id, ts, raw_body, self.machine_secret)

        headers = {
            "Content-Type": "application/json",
            "X-ComputeCoin-Machine": self.machine_id,
            "X-ComputeCoin-Timestamp": str(ts),
            "X-ComputeCoin-Signature": sig,
        }

        url = f"{self.base_url}/v1/telemetry/machine"
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport

        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(url, content=raw_body, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            # On network or 5xx failure, push back items to buffer so they are not dropped
            for pt in reversed(to_send):
                self._buffer.appendleft(pt)
            raise exc
