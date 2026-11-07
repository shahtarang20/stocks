"""Governance inside the Windows supervisor (cross-platform plan W5 -> W2, decision X13).

``build_governance`` assembles the pieces that already exist (``policy.GovernancePolicy`` and ``GovernanceEngine``,
``integration.GovernedGuard``, the probes, ``snapshot.SnapshotProbe``) into one object the supervisor consults:

* its decision is the supervisor's ``availability_source`` (online / yielding / maintenance / draining; every non-online
  state is sent to the API as ``throttled`` with the real state in ``health.availability_detail``);
* signals come from the PAL-style probes: power / reboot-pending / battery from ``WindowsPowerState`` (works from the
  service), and the owner-activity signals from the signed snapshot file the user-session helper writes (the service is in
  session 0 and cannot see the desktop), or straight from the Win32 probes when ``use_session_helper`` is off (a
  supervisor run from the owner's own console session);
* ``SleepInhibitor.sync`` follows ``Decision.prevent_sleep`` and is additionally forced off whenever the supervisor knows
  no job is running, so the machine is only kept awake while a job runs. The supervisor calls ``on_job_event`` from the
  control channel's ``job_started`` / ``job_finished`` / ``usage`` messages.

Fail closed (L1): an exception while deciding reports ``yielding`` (never ``online``) and releases the sleep request; a
probe that fails is recorded by the engine as a probe error, which blocks claims; a stale, unauthenticated or missing
snapshot is such an error. Nothing here ever turns a "no" into a "yes".
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .integration import GovernedGuard, JobTracker, make_collector, utc_now
from .policy import Decision, GovernanceEngine, GovernancePolicy, State

logger = logging.getLogger("computecoin_agent.windows.governance")


class SupervisorJobView(JobTracker):
    """A ``JobTracker`` that reads the supervisor's own count of running jobs (reset when the runner dies), so the engine
    and the supervisor can never disagree about whether a job is running."""

    def __init__(self, active_jobs: Callable[[], int]) -> None:
        super().__init__()
        self._active = active_jobs

    @property
    def running(self) -> bool:
        try:
            return int(self._active()) > 0
        except Exception:  # noqa: BLE001 - unknown counts as "a job may be running" (never as idle)
            return True


class SupervisorGovernance:
    def __init__(
        self,
        policy: GovernancePolicy,
        *,
        activity: Any = None,
        power: Any = None,
        owner_gpu: Any = None,
        inhibitor: Any = None,
        active_jobs: Callable[[], int],
        on_stop_job: Optional[Callable[[Decision], None]] = None,
        clock: Callable[[], Any] = utc_now,
    ) -> None:
        self.engine = GovernanceEngine(policy)
        self.jobs = SupervisorJobView(active_jobs)
        self._inhibitor = inhibitor
        self.guard = GovernedGuard(
            self.engine,
            make_collector(activity, power, owner_gpu),
            jobs=self.jobs,
            on_stop_job=on_stop_job,
            on_decision=self._on_decision,
            clock=clock,
        )

    # -- sleep prevention ---------------------------------------------------------------------------------------- #
    def _sync_sleep(self, wanted: bool) -> None:
        if self._inhibitor is None:
            return
        try:
            self._inhibitor.sync(bool(wanted))
        except Exception as exc:  # noqa: BLE001
            logger.warning("sleep inhibitor sync failed: %s", type(exc).__name__)

    def _on_decision(self, decision: Decision) -> None:
        # Only while a job really runs: the engine's flag AND the supervisor's own job count.
        self._sync_sleep(bool(decision.prevent_sleep and self.jobs.running))

    # -- supervisor hooks ------------------------------------------------------------------------------------------ #
    def source(self) -> "tuple[str, dict[str, Any]]":
        """The supervisor's ``availability_source``: ``(state, detail)``. Never raises; an error is ``yielding``."""
        try:
            decision = self.guard.decide()
            state = State(decision.state).value
        except Exception as exc:  # noqa: BLE001 - an unknown governance answer must block, not allow
            logger.warning("governance decision failed: %s", type(exc).__name__)
            self._sync_sleep(False)
            return "yielding", {"code": "governance_error", "causes": ["governance_error"]}
        return state, {"code": str(decision.code), "causes": list(decision.causes)}

    def on_job_event(self) -> None:
        """A job started or finished (control-channel message): re-decide now so sleep prevention follows the job without
        waiting for the next supervisor tick. Never raises."""
        try:
            self.guard.decide()
        except Exception as exc:  # noqa: BLE001
            logger.warning("governance re-evaluation failed: %s", type(exc).__name__)
            self._sync_sleep(False)

    def close(self) -> None:
        self._sync_sleep(False)


def build_governance(
    config: Any,
    settings: Any,
    layout: Any,
    store: Any,
    *,
    active_jobs: Callable[[], int],
    on_stop_job: Optional[Callable[[Decision], None]] = None,
    api: Any = None,
    activity: Any = None,
    power: Any = None,
    owner_gpu: Any = None,
    inhibitor: Any = None,
) -> Optional[SupervisorGovernance]:
    """The governance object for a supervisor, or None when the owner explicitly set ``governance_enabled = false``.

    ``api`` / ``activity`` / ``power`` / ``owner_gpu`` / ``inhibitor`` are injectable (tests pass fakes); the defaults need
    Windows. A policy the config cannot express (bad schedule, negative time) raises ValueError, so the service does not
    start with a policy nobody chose."""
    if getattr(config, "governance_enabled", None) is False:
        logger.warning("resource governance is switched off by the owner (governance_enabled = false)")
        return None
    policy = GovernancePolicy.from_config(config)

    from . import probes
    from .helper import snapshot_path
    from .snapshot import SNAPSHOT_KEY_NAME, SnapshotProbe

    if api is None and (power is None or inhibitor is None or activity is None):
        api = probes.Win32Api()  # raises ProbeUnsupported off Windows: the supervisor then does not start (fail closed)
    if power is None:
        power = probes.WindowsPowerState(api)
    if inhibitor is None:
        inhibitor = probes.SleepInhibitor(api)
    if activity is None:
        if settings.use_session_helper:
            probe = SnapshotProbe.authenticated(
                snapshot_path(layout),
                lambda: store.load(SNAPSHOT_KEY_NAME),
                max_age_seconds=float(settings.snapshot_max_age_seconds),
                no_session=lambda: not probes.interactive_session_present(api),
            )
            activity = probe
            owner_gpu = owner_gpu or probe
        else:
            activity = probes.WindowsActivityProbe(api)
            owner_gpu = owner_gpu or probes.WindowsOwnerGpuProbe(activity)
    return SupervisorGovernance(
        policy, activity=activity, power=power, owner_gpu=owner_gpu, inhibitor=inhibitor,
        active_jobs=active_jobs, on_stop_job=on_stop_job,
    )
