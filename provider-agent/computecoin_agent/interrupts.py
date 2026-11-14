"""A stop forced by the HOST'S OWNER (as opposed to the customer cancelling, or the GPU overheating).

The owner of a shared computer needs it back: a full-screen game starts, "stop now" is pressed, a laptop leaves the
charger. The running customer job cannot be paused, so it is stopped cleanly and reported as ``host_interrupted``. The
platform then re-runs it on another computer for free and never charges the customer for the stopped attempt (server:
``HOST_CAUSED_ERROR_CODES``). Without this the runner process was simply killed, the lease ran out about a minute later,
and the customer waited for nothing.

``InterruptRegistry`` is the one place that knows which jobs are running: the runner registers each job's event when it
starts and removes it when it ends; whoever needs the computer back (the Windows supervisor over the control channel, or
the Linux owner-activity watcher in the same process) calls :meth:`interrupt_all`.
"""

from __future__ import annotations

import threading
from typing import Optional


#: The error code reported for a job the owner stopped. The API's ``HOST_CAUSED_ERROR_CODES`` must contain it, or the
#: platform would treat the stop as the customer's failure (``tests/e2e/test_host_interrupt_contract.py`` guards this).
HOST_INTERRUPTED_CODE = "host_interrupted"


class InterruptRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._reason: Optional[str] = None

    def register(self, job_id: str) -> threading.Event:
        """A fresh event for ``job_id``. If an interruption is already in force the event starts set: a job that begins
        in the middle of one is stopped at once instead of running into the owner's game."""
        event = threading.Event()
        with self._lock:
            self._events[job_id] = event
            if self._reason is not None:
                event.set()
        return event

    def unregister(self, job_id: str) -> None:
        with self._lock:
            self._events.pop(job_id, None)
            if not self._events:
                self._reason = None  # nothing left running: the interruption has done its work

    def interrupt_all(self, reason: str = "owner_needs_computer") -> int:
        """Ask every running job to stop. Returns how many were running. Safe to call with none running."""
        with self._lock:
            events = list(self._events.values())
            self._reason = (str(reason or "owner_needs_computer"))[:200] if events else None
        for event in events:
            event.set()
        return len(events)

    def running(self) -> int:
        with self._lock:
            return len(self._events)

    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason


REGISTRY = InterruptRegistry()
