"""Send this computer's REAL GPU readings to the platform, so the host can see their usage live.

``telemetry.TelemetryCollector`` could always buffer samples and POST them to ``/v1/telemetry/machine``, but nothing ever
started it, so the platform had no readings and a host's usage page would have been empty. This runs it.

Two rules:

* NEVER send a made-up reading. When there is no real GPU, ``sample_gpu_metrics`` returns a placeholder tagged
  ``simulated_probe``; it is dropped here (the job monitor and the Windows supervisor do the same). A host with no GPU
  therefore sends nothing, and the page says so instead of showing an invented 25 % load.
* NEVER let this stop the agent. A failed read or a failed upload is swallowed; unsent readings stay in the collector's
  bounded buffer (oldest dropped first) and go out with the next batch.

What is sent: per GPU, utilisation, memory used and total, temperature, power and fan speed. No process names, no job data.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

SIMULATED_MODE = "simulated_probe"


def real_gpu_samples() -> list:
    """Real readings only: the placeholder sample for a machine with no GPU is filtered out."""
    from .telemetry import sample_gpu_metrics

    return [s for s in sample_gpu_metrics() if (getattr(s, "extra_metrics", None) or {}).get("mode") != SIMULATED_MODE]


class TelemetryLoop:
    def __init__(
        self,
        collector: Any,
        *,
        sample_interval: float = 30.0,
        flush_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._collector = collector
        self._sample_interval = max(1.0, float(sample_interval))
        self._flush_interval = max(1.0, float(flush_interval))
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_flush = float("-inf")

    def tick(self) -> None:
        """One round: read the GPUs, and send what is buffered if a minute has passed. Never raises."""
        try:
            self._collector.sample_now()
        except Exception:  # noqa: BLE001 - a failed read must never stop the agent
            pass
        now = self._clock()
        if now - self._last_flush >= self._flush_interval:
            self._last_flush = now
            try:
                self._collector.flush()
            except Exception:  # noqa: BLE001 - unsent points stay buffered and go with the next batch
                pass

    def start(self) -> "TelemetryLoop":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="telemetry-loop", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._sample_interval)


def build_telemetry_loop(base_url: str, machine_id: str, machine_secret: str) -> TelemetryLoop:
    """The loop for this machine, reading real GPUs only."""
    from .telemetry import TelemetryCollector

    collector = TelemetryCollector(machine_id, machine_secret, base_url=base_url, sampler=real_gpu_samples)
    return TelemetryLoop(collector)
