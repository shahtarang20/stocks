"""Glue between the governance engine and the agent's existing claim loop (B6 ``thermal_guard`` interface).

``poll_loop(thermal_guard=...)`` only calls ``may_claim()`` and the executor only calls ``evaluate`` / ``read_max_temp`` on
it, so :class:`GovernedGuard` wraps the thermal guard: a claim needs BOTH the thermal guard and the governance engine to
allow it, and every other attribute is delegated to the thermal guard unchanged. Wiring it into ``cli.py`` is the
supervisor's job (W4); nothing here edits the existing loop.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Callable, Optional

from .policy import Decision, GovernanceEngine, Signals, collect_signals

logger = logging.getLogger(__name__)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def make_collector(activity: Any = None, power: Any = None, owner_gpu: Any = None) -> Callable[[dt.datetime, bool], Signals]:
    """The ``collect`` argument of :class:`GovernedGuard`, built from the PAL ``ActivityProbe`` / ``PowerState`` and the owner-GPU probe."""
    return lambda now, job_running: collect_signals(now, activity=activity, power=power, owner_gpu=owner_gpu, job_running=job_running)


class JobTracker:
    """Counts running jobs. The control channel (W4) calls ``begin`` on ``job_started`` and ``end`` on ``job_finished``."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def begin(self) -> None:
        with self._lock:
            self._n += 1

    def end(self) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._n > 0


class GovernedGuard:
    def __init__(
        self,
        engine: GovernanceEngine,
        collect: Callable[[dt.datetime, bool], Signals],
        *,
        thermal: Any = None,
        jobs: Optional[JobTracker] = None,
        on_stop_job: Optional[Callable[[Decision], None]] = None,
        on_decision: Optional[Callable[[Decision], None]] = None,
        clock: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        """``collect(now, job_running)`` builds Signals (normally ``functools.partial(collect_signals, activity=...,
        power=..., owner_gpu=...)`` adapted to this two-argument shape)."""
        self.engine = engine
        self._collect = collect
        self._thermal = thermal
        self.jobs = jobs or JobTracker()
        self._on_stop_job = on_stop_job
        self._on_decision = on_decision  # e.g. SleepInhibitor.sync(decision.prevent_sleep) and the tray status
        self._clock = clock

    def decide(self) -> Decision:
        now = self._clock()
        try:
            signals = self._collect(now, self.jobs.running)
        except Exception as exc:  # noqa: BLE001 - never let a collector bug open the gate
            logger.warning("governance signal collection failed: %s", type(exc).__name__)
            signals = Signals(now=now, job_running=self.jobs.running, probe_errors=("collector",))
        decision = self.engine.evaluate(signals)
        if decision.stop_job and self._on_stop_job is not None:
            try:
                self._on_stop_job(decision)
            except Exception:  # noqa: BLE001
                logger.exception("stop-job callback failed")
        if self._on_decision is not None:
            try:
                self._on_decision(decision)
            except Exception:  # noqa: BLE001
                logger.exception("decision callback failed")
        return decision

    def may_claim(self) -> bool:
        decision = self.decide()
        thermal_ok = True if self._thermal is None else bool(self._thermal.may_claim())
        return decision.claim_allowed and thermal_ok

    def availability(self) -> str:
        """For ``heartbeat_loop(availability_fn=...)``: "online" only when both guards allow work."""
        decision = self.decide()
        thermal = "online" if self._thermal is None else self._thermal.availability()
        return "online" if decision.claim_allowed and thermal == "online" else "throttled"

    def health(self) -> dict:
        d = self.engine.last() or self.decide()
        return d.heartbeat_health()

    def __getattr__(self, name: str) -> Any:  # thermal guard surface used by the executor (evaluate, read_max_temp, ...)
        thermal = self.__dict__.get("_thermal")
        if thermal is None:
            raise AttributeError(name)
        return getattr(thermal, name)
