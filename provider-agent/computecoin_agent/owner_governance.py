"""Linux: step aside when the owner is busy (decision X13), for a laptop or a desktop that is also somebody's computer.

This is the Linux counterpart of ``windows/governance/wiring.py``. It reuses the SAME rules engine
(``windows/governance/policy.py``: pure Python, no Windows calls) and gives it Linux probes (``owner_activity.py``).

What it does, with the default policy:

* The owner is at the computer (input within 5 minutes), another program is using the GPU, the computer is on battery,
  or GPU memory is short: NO NEW JOBS are taken, and the heartbeat says ``throttled`` so the scheduler skips this machine.
* A job that is already running is NOT killed: it finishes. (The customer's work is the thing we must not spoil.)
* The owner can pause hosting any time with ``computecoin-agent pause`` ("stop now"): then the running job is stopped
  cleanly, reported ``host_interrupted`` (the platform re-runs it elsewhere, free, no charge to the customer), and nothing
  new is taken until ``computecoin-agent resume``.
* Coming back online needs the machine to stay quiet for ``governance_resume_clear_seconds`` (default 60), so it cannot flap.

It is OFF unless the owner sets ``governance_enabled = true`` in ``agent.toml`` (a server must keep working exactly as
before). A server with no desktop sets ``governance_headless = true`` to keep battery and schedule rules but not the
"is somebody at the keyboard" ones.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import interrupts
from .owner_activity import (
    LINUX_IGNORED_PROCESSES,
    LinuxActivityProbe,
    NvidiaGpuUsers,
)
from .platform.linux import LinuxPower
from .windows.governance.policy import (  # pure Python: shared by Windows and Linux
    Decision,
    GovernanceEngine,
    GovernancePolicy,
    State,
    collect_signals,
)

#: What Linux can read. A "full-screen app" and a "pending reboot" cannot be read, so they must never block by being unknown.
_LINUX_UNKNOWN_BLOCKS = ("idle", "battery")

OWNER_STOP_FILENAME = "owner-stop"


def owner_stop_path(config_dir: Optional[Path] = None) -> Path:
    """The marker the ``pause`` command writes and the running agent watches (``resume`` removes it)."""
    if config_dir is None:
        from .config import default_config_dir

        config_dir = default_config_dir()
    return Path(config_dir) / OWNER_STOP_FILENAME


def linux_policy(config: Any) -> GovernancePolicy:
    """The policy from the ``governance_*`` settings, adjusted for what Linux can actually observe."""
    base = GovernancePolicy.from_config(config)
    extras = {str(p).strip().lower() for p in (getattr(config, "governance_ignored_processes", []) or []) if str(p).strip()}
    unknown = tuple(k for k in base.unknown_blocks if k in _LINUX_UNKNOWN_BLOCKS)
    return dataclasses.replace(
        base,
        unknown_blocks=unknown,
        ignored_processes=frozenset(LINUX_IGNORED_PROCESSES | extras),
        preempt_on_fullscreen=False,  # full-screen cannot be detected on Linux, so there is nothing to preempt on
    )


class LinuxGovernance:
    """Decides, from Linux probes, whether this machine may take work right now."""

    def __init__(
        self,
        policy: GovernancePolicy,
        *,
        activity: Any = None,
        power: Any = None,
        owner_gpu: Any = None,
        stop_file: Optional[Path] = None,
        now_fn: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
        clock: Callable[[], float] = time.monotonic,
        interrupt: Callable[[str], Any] = interrupts.REGISTRY.interrupt_all,
        running: Callable[[], int] = interrupts.REGISTRY.running,
        min_interval_seconds: float = 2.0,
    ) -> None:
        self.engine = GovernanceEngine(policy)
        self._activity, self._power, self._gpu = activity, power, owner_gpu
        self._stop_file = stop_file
        self._now_fn, self._clock = now_fn, clock
        self._interrupt, self._running = interrupt, running
        self._min_interval = float(min_interval_seconds)
        self._lock = threading.RLock()
        self._last: Optional[Decision] = None
        self._last_at = float("-inf")

    @classmethod
    def from_config(cls, config: Any, *, stop_file: Optional[Path] = None, **overrides: Any) -> "LinuxGovernance":
        extras = frozenset(str(p).strip().lower() for p in (getattr(config, "governance_ignored_processes", []) or []))
        gpu = NvidiaGpuUsers(ignored=LINUX_IGNORED_PROCESSES | extras)
        kwargs: dict[str, Any] = dict(
            activity=LinuxActivityProbe(), power=LinuxPower(), owner_gpu=gpu,
            stop_file=stop_file if stop_file is not None else owner_stop_path(),
        )
        kwargs.update(overrides)
        return cls(linux_policy(config), **kwargs)

    # -- the owner's pause switch ---------------------------------------------------------------------------- #
    def _sync_owner_pause(self) -> None:
        path = self._stop_file
        if path is None:
            return
        try:
            exists = path.exists()
            mtime = path.stat().st_mtime if exists else None
        except OSError:
            return  # cannot tell: leave the current state as it is
        if exists:
            if not self.engine.stopped_by_owner:
                when = dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc) if mtime else self._now_fn()
                self.engine.stop_now(when)  # a "hold" setting counts from the moment the owner paused
        elif self.engine.stopped_by_owner:
            self.engine.resume()

    # -- evaluation -------------------------------------------------------------------------------------------- #
    def evaluate(self, *, force: bool = False) -> Decision:
        """The current decision. Cheap to call often: the probes run at most every ``min_interval_seconds``."""
        with self._lock:
            now_m = self._clock()
            if not force and self._last is not None and now_m - self._last_at < self._min_interval:
                return self._last
            self._sync_owner_pause()
            signals = collect_signals(
                self._now_fn(),
                activity=self._activity,
                power=self._power,
                owner_gpu=self._gpu,
                job_running=self._running() > 0,
            )
            decision = self.engine.evaluate(signals)
            self._last, self._last_at = decision, now_m
        if decision.stop_job:
            self._interrupt(f"owner:{decision.code}")  # stop the running job(s) cleanly: host_interrupted, free retry
        return decision

    # -- what the agent asks ----------------------------------------------------------------------------------- #
    def availability(self) -> str:
        """``"online"`` or ``"throttled"`` for the heartbeat (the scheduler skips a throttled machine)."""
        return self.evaluate().heartbeat_availability

    def claim_block_reason(self) -> Optional[str]:
        """None when new work may be claimed, else the reason code (for the log)."""
        decision = self.evaluate()
        return None if decision.state is State.ONLINE else decision.code

    def health_extras(self) -> dict:
        """The additive heartbeat health block: ``availability_detail = {state, code}`` (the admin console shows it)."""
        return self.evaluate().heartbeat_health()
