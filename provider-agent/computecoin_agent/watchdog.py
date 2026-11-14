"""An independent heat watchdog, and cleanup of job containers an earlier agent left behind.

Why this exists. The agent's own thermal check (``executor._thermal_watch``) lives INSIDE the agent process. If the agent
crashes or hangs, a customer's container keeps the GPU busy and nothing is left to notice that the card is overheating.
Two safeguards cover that:

* :func:`reap_orphans` runs when the agent starts: any ``ccoin-*`` container (and egress network) still there belongs to
  an agent that is gone, so it is removed before new work is taken;
* :class:`HeatWatchdog` runs as a SEPARATE process (``computecoin-agent watchdog``). It only acts when the card has
  been over the hard limit (or has had an unreadable sensor, see ``thermal.SENSOR_LOST``) for ``grace_seconds`` longer than
  the agent's own check would need, so a healthy agent always acts first and reports the job properly
  (``thermal_shutdown``: the host's doing, never charged to the customer). The watchdog is the last line of defence.

The watchdog never starts anything, never reads a job's contents and never talks to the network: it lists containers by
name prefix, reads the same temperature the agent reads, and removes containers. It cannot protect the hardware from
anything the GPU's own firmware does not already handle: it narrows the window in which a dead agent leaves work running.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .thermal import HARD_STOP, SENSOR_LOST, ThermalGuard

logger = logging.getLogger(__name__)

#: Containers the agent starts for customers' work: jobs, sessions, services, and the egress proxy of a job.
CONTAINER_PREFIXES = ("ccoin-job-", "ccoin-sess-", "ccoin-proxy-", "cc-svc-")
#: What a restarting ``start`` clears. Sessions and services are NOT here: the separate ``sessions`` command owns the
#: ccoin-sess- containers and may be running right now. (The heat watchdog still removes all of them: that is an emergency.)
STARTUP_REAP_PREFIXES = ("ccoin-job-", "ccoin-proxy-")
NETWORK_PREFIX = "ccoin-net-"
_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")

#: A docker call takes the argv and returns stdout, or None when it failed or docker is missing. It never raises.
DockerCall = Callable[[list], Optional[str]]

DEFAULT_GRACE_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 5.0
ALIVE_FILE_NAME = "watchdog.alive"


def _docker(argv: list, timeout: float = 30.0) -> Optional[str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def list_job_containers(docker: DockerCall = _docker, *, include_stopped: bool = False,
                        prefixes: tuple = CONTAINER_PREFIXES) -> list:
    """``[(id, name), ...]`` for every container this agent starts for customers. ``[]`` when there are none or docker
    cannot be asked. Matching is on the NAME PREFIX (docker's own name filter matches anywhere in the name)."""
    argv = ["docker", "ps"] + (["-a"] if include_stopped else []) + ["--no-trunc", "--format", "{{.ID}}\t{{.Names}}"]
    text = docker(argv)
    out = []
    for line in (text or "").splitlines():
        cid, _, names = line.strip().partition("\t")
        if not _ID_RE.match(cid):
            continue
        for name in names.split(","):
            if name.strip().startswith(prefixes):
                out.append((cid, name.strip()))
                break
    return out


def remove_containers(ids: list, docker: DockerCall = _docker) -> bool:
    """Kill and remove the given containers. True when docker accepted the command."""
    ids = [i for i in ids if _ID_RE.match(str(i))]
    if not ids:
        return True
    return docker(["docker", "rm", "-f", *ids]) is not None


@dataclass(frozen=True)
class ReapResult:
    containers: int = 0
    networks: int = 0


def reap_orphans(docker: DockerCall = _docker) -> ReapResult:
    """At agent start: remove the job containers and egress networks an earlier run left behind.

    Only call this before the agent takes work: it removes containers that are running right now. Session and service
    containers are left alone (see ``STARTUP_REAP_PREFIXES``)."""
    try:
        found = list_job_containers(docker, include_stopped=True, prefixes=STARTUP_REAP_PREFIXES)
        containers = len(found) if found and remove_containers([c for c, _ in found], docker) else 0
        networks = 0
        for name in (docker(["docker", "network", "ls", "--format", "{{.Name}}"]) or "").split():
            if name.startswith(NETWORK_PREFIX) and docker(["docker", "network", "rm", name]) is not None:
                networks += 1
        if containers or networks:
            logger.warning("removed %d container(s) and %d network(s) left behind by an earlier agent run", containers, networks)
        return ReapResult(containers, networks)
    except Exception as exc:  # cleanup must never stop the agent from starting
        logger.warning("orphan cleanup failed: %s", type(exc).__name__)
        return ReapResult()


class HeatWatchdog:
    """Removes the customers' containers when the GPU has been dangerously hot, or blind, for longer than the agent
    needs to react. Call :meth:`step` on a timer."""

    def __init__(
        self,
        guard: ThermalGuard,
        *,
        docker: DockerCall = _docker,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not grace_seconds >= 0:
            raise ValueError("grace_seconds must not be negative")
        self._guard = guard
        self._docker = docker
        self._grace = float(grace_seconds)
        self._clock = clock
        self._bad_since: Optional[float] = None
        self.kills = 0

    def step(self) -> str:
        """One look. ``"ok"``, ``"watching"`` (bad, within the grace period) or ``"killed"``."""
        state = self._guard.check()
        now = self._clock()
        if state not in (HARD_STOP, SENSOR_LOST):
            self._bad_since = None
            return "ok"
        if self._bad_since is None:
            self._bad_since = now
        if now - self._bad_since < self._grace:
            return "watching"
        found = list_job_containers(self._docker)
        if found:
            remove_containers([c for c, _ in found], self._docker)
            self.kills += len(found)
            logger.error("heat watchdog (%s for %.0f s): removed %d container(s): %s", state, now - self._bad_since,
                         len(found), ", ".join(n for _, n in found))
        return "killed"


# ---- the separate process -------------------------------------------------------------------------------------------
def alive_file() -> Path:
    from .platform import get_platform

    return Path(get_platform().paths.data_dir()) / ALIVE_FILE_NAME


def is_alive(path: Optional[Path] = None, *, max_age: Optional[float] = None, now: Callable[[], float] = time.time) -> bool:
    """True when a watchdog wrote its file recently. The file holds the watchdog's own poll interval, so a slow poll is not
    mistaken for a dead watchdog (``max_age`` defaults to four polls). A file that cannot be read means "not running"."""
    target = path or alive_file()
    try:
        age = now() - os.path.getmtime(target)
        if max_age is None:
            try:
                poll = float(Path(target).read_text().strip())
            except (OSError, ValueError):
                poll = DEFAULT_POLL_SECONDS
            max_age = max(1.0, poll) * 4
    except OSError:
        return False
    return 0 <= age <= max_age


def run_forever(
    guard: ThermalGuard,
    stop: threading.Event,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    docker: DockerCall = _docker,
    path: Optional[Path] = None,
) -> None:
    """The watchdog's loop: look, touch the alive file, wait. A failing step is logged and never ends the loop."""
    dog = HeatWatchdog(guard, docker=docker, grace_seconds=grace_seconds)
    marker = path or alive_file()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    while not stop.is_set():
        try:
            dog.step()
        except Exception as exc:  # a broken sensor or docker must not end the last line of defence
            logger.warning("heat watchdog step failed: %s", type(exc).__name__)
        try:
            marker.write_text(f"{poll_seconds:g}")  # also the "I am alive" mark: its modification time is what is checked
        except OSError:
            pass
        stop.wait(poll_seconds)


def spawn_detached() -> bool:
    """Start ``computecoin-agent watchdog`` as its own process (own session, no terminal). True when it was started."""
    try:
        subprocess.Popen(
            [sys.executable, "-c", "from computecoin_agent.cli import app; app()", "watchdog"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return True
    except OSError as exc:
        logger.warning("could not start the heat watchdog: %s", type(exc).__name__)
        return False


def keep_running(stop: threading.Event, *, every: float = 30.0, ensure: Optional[Callable[[], str]] = None) -> None:
    """Loop for a background thread in the agent: if the watchdog has died (or was stopped together with an agent that
    crashed under systemd), start it again. Never raises."""
    check = ensure or ensure_running
    while not stop.wait(every):
        try:
            if check() == "started":
                logger.warning("the heat watchdog had stopped; started it again")
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchdog check failed: %s", type(exc).__name__)


def ensure_running(*, alive: Optional[Callable[[], bool]] = None, spawn: Callable[[], bool] = spawn_detached) -> str:
    """``"already_running"``, ``"started"`` or ``"failed"``. Two agents starting together may start two watchdogs: that is
    harmless (each only removes containers when the card has been too hot for the grace period)."""
    if (alive or is_alive)():
        return "already_running"
    return "started" if spawn() else "failed"


SYSTEMD_UNIT = """\
[Unit]
Description=Computecoin heat watchdog (removes customer containers if the GPU overheats and the agent is gone)
After=docker.service

[Service]
Type=simple
ExecStart={exe} watchdog
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def systemd_unit(exe: str) -> str:
    """A ready-to-install unit. A SEPARATE unit matters: a watchdog started by the agent's own unit is stopped with it."""
    return SYSTEMD_UNIT.format(exe=exe)
