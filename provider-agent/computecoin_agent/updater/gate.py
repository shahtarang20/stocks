"""The claim gate that makes "never update while a job runs" an atomic fact rather than a hope.

The claim loop takes a SLOT before it asks the server for work and gives it back when the job (or an empty claim) is over:

    if gate.acquire_job_slot():
        try:   claim and run
        finally: gate.release_job_slot()

The updater may begin only when no slot is held, and while it runs (or while a drain was requested) no slot is handed out.
A host below the forced-update floor hands out no slot at all until it has updated.
"""

from __future__ import annotations

import threading
from typing import Optional

from .version import compare


class UpdateGate:
    def __init__(self, current_version: str, floor: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._slots = 0
        self._draining = False
        self._updating = False
        self._blocked = False
        self.current_version = current_version
        self.floor = floor

    # ---- claim loop side ---- #
    def acquire_job_slot(self) -> bool:
        with self._lock:
            if self._blocked or self._updating or self._draining or self._below_floor_locked():
                return False
            self._slots += 1
            return True

    def release_job_slot(self) -> None:
        with self._lock:
            self._slots = max(0, self._slots - 1)

    def may_claim(self) -> bool:
        """Advisory (heartbeat availability); ``acquire_job_slot`` is the real gate."""
        with self._lock:
            return not (self._blocked or self._updating or self._draining or self._below_floor_locked())

    @property
    def jobs_running(self) -> bool:
        with self._lock:
            return self._slots > 0

    # ---- updater side ---- #
    def request_drain(self) -> None:
        with self._lock:
            self._draining = True

    def cancel_drain(self) -> None:
        with self._lock:
            self._draining = False

    def try_begin_update(self) -> bool:
        with self._lock:
            if self._slots > 0 or self._updating:
                return False
            self._updating = True
            return True

    def end_update(self) -> None:
        with self._lock:
            self._updating = False

    def block(self) -> None:
        """Permanent for this process: the host is in an unknown state (a failed rollback) and must claim nothing."""
        with self._lock:
            self._blocked = True

    def set_floor(self, floor: Optional[str]) -> None:
        with self._lock:
            self.floor = floor

    def set_current(self, version: str) -> None:
        with self._lock:
            self.current_version = version

    @property
    def below_floor(self) -> bool:
        with self._lock:
            return self._below_floor_locked()

    def _below_floor_locked(self) -> bool:
        return self.floor is not None and compare(self.current_version, self.floor) < 0
