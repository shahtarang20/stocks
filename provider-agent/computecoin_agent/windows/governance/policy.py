"""Resource governance policy engine (cross-platform plan W5, decision X13: the host yields to its owner).

Pure Python: no OS calls, no threads, no clock of its own. Every input arrives in a :class:`Signals` value, so the engine
is tested on Linux with fakes and is equally usable on a Linux desktop (X13 applies there too).

Output of :meth:`GovernanceEngine.evaluate` is a :class:`Decision`:

==============  ====================================================================================================
``online``      the machine may claim and run work
``yielding``    the owner has the machine (foreground GPU app, recent input, full-screen app, battery, outside the
                schedule, VRAM reserve, ``stop now``): no new claims
``maintenance`` the OS needs the machine (Windows Update reboot pending, sleep/hibernate in progress): no new claims
``draining``    one of the above is true while a job is still running: that job finishes (or is stopped only by an
                explicit ``stop now``), nothing new is claimed
==============  ====================================================================================================

Defaults are conservative (X13, L1): yield to the owner ON, never during a full-screen app, never on battery, and an
UNKNOWN signal blocks (a probe that cannot tell is never read as "the owner is away"). A headless host opts out of the
activity signals with ``GovernancePolicy.headless()``.

Hysteresis: entering a non-online state is immediate (the owner comes first); returning to ``online`` needs every cause
to stay clear for ``resume_clear_seconds`` without a break, and a clock that goes backwards restarts that count. So the
state cannot flap faster than once per ``resume_clear_seconds``.

Heartbeat mapping: the API accepts ``online | maintenance | offline | throttled`` and treats ``maintenance`` as a STICKY
owner hold that a later ``online`` heartbeat does not undo (apps/api/app/api/routers/agents.py heartbeat). The agent
therefore never reports ``maintenance`` itself: every non-online state is reported as ``throttled`` (the scheduler skips
it and the next ``online`` heartbeat resumes it) and the real state travels in the additive health field
``availability_detail`` (see :meth:`Decision.heartbeat_health`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import re
import threading
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

try:  # zoneinfo needs the ``tzdata`` package on Windows; a missing zone fails CLOSED (the window never matches)
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9 is not supported by the agent
    ZoneInfo = None  # type: ignore[assignment,misc]
    ZoneInfoNotFoundError = KeyError  # type: ignore[assignment,misc]


class State(str, enum.Enum):
    ONLINE = "online"
    YIELDING = "yielding"
    MAINTENANCE = "maintenance"
    DRAINING = "draining"


# What the heartbeat ``availability`` field says for each state (see module docstring: never "maintenance").
HEARTBEAT_AVAILABILITY: Mapping[State, str] = {
    State.ONLINE: "online",
    State.YIELDING: "throttled",
    State.MAINTENANCE: "throttled",
    State.DRAINING: "throttled",
}

# Processes that always hold some GPU memory on a Windows desktop and say nothing about the owner using the GPU.
DEFAULT_IGNORED_PROCESSES = frozenset({"dwm.exe", "explorer.exe", "csrss.exe", "searchhost.exe", "textinputhost.exe"})

# Signals that, when UNKNOWN (the probe answered None or failed), block claims. ``sleep`` is not here: a pending sleep
# cannot be known ahead (the OS announces it with an event), so None means "no event seen".
UNKNOWN_BLOCKS_DEFAULT = ("fullscreen", "idle", "battery", "reboot")

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------------- #
# Schedule windows
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class ScheduleWindow:
    """Allowed hours: ``days`` (0 = Monday .. 6 = Sunday) from ``start`` to ``end`` local time in ``timezone``.

    ``end <= start`` means the window crosses midnight and belongs to the day it STARTS on (22:00-07:00 on Friday runs
    until Saturday 07:00). Local time comes from ``zoneinfo``, so daylight saving shifts are honoured.
    """

    days: frozenset
    start: dt.time
    end: dt.time
    timezone: str

    def contains(self, now: dt.datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if ZoneInfo is None:
            return False
        try:
            local = now.astimezone(ZoneInfo(self.timezone))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return False  # unknown zone: fail closed, the window never matches
        t = local.time().replace(second=0, microsecond=0, tzinfo=None)
        day = local.weekday()
        if self.start < self.end:
            return day in self.days and self.start <= t < self.end
        # crosses midnight (or start == end, read as "start to start next day": the whole day from ``start`` on)
        prev_day = (day - 1) % 7
        return (day in self.days and t >= self.start) or (prev_day in self.days and t < self.end)

    @classmethod
    def parse(cls, raw: Mapping[str, Any], default_timezone: Optional[str] = None) -> "ScheduleWindow":
        """``{"days": ["mon", ...] | [0, ...] | "daily" | "weekdays" | "weekend", "start": "22:00", "end": "07:00",
        "timezone": "Europe/Berlin"}``. Raises ValueError on anything malformed (an owner typo is refused loudly)."""
        if not isinstance(raw, Mapping):
            raise ValueError("schedule window must be a table")
        unknown = set(raw) - {"days", "start", "end", "timezone"}
        if unknown:
            raise ValueError(f"unknown schedule field(s): {sorted(unknown)}")
        days = _parse_days(raw.get("days", "daily"))
        start, end = _parse_time(raw.get("start")), _parse_time(raw.get("end"))
        tz = raw.get("timezone") or default_timezone
        if not isinstance(tz, str) or not tz.strip():
            raise ValueError("schedule window needs a timezone (IANA name, e.g. 'Europe/Berlin')")
        tz = tz.strip()
        if ZoneInfo is not None:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                raise ValueError(f"unknown timezone {tz!r} (on Windows the 'tzdata' package must be installed)")
        return cls(days=days, start=start, end=end, timezone=tz)


def _parse_time(value: Any) -> dt.time:
    if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
        raise ValueError("schedule start/end must be 'HH:MM' (24 hour)")
    h, m = value.strip().split(":")
    return dt.time(int(h), int(m))


def _parse_days(value: Any) -> frozenset:
    if isinstance(value, str):
        key = value.strip().lower()
        if key == "daily":
            return frozenset(range(7))
        if key == "weekdays":
            return frozenset(range(5))
        if key == "weekend":
            return frozenset({5, 6})
        value = [key]
    if not isinstance(value, Iterable) or isinstance(value, (bytes, Mapping)):
        raise ValueError("schedule days must be a list")
    out = set()
    for item in value:
        if isinstance(item, bool):
            raise ValueError("invalid schedule day")
        if isinstance(item, int):
            if not 0 <= item <= 6:
                raise ValueError("schedule day numbers are 0 (Monday) to 6 (Sunday)")
            out.add(item)
        elif isinstance(item, str) and item.strip().lower()[:3] in _DAY_NAMES:
            out.add(_DAY_NAMES.index(item.strip().lower()[:3]))
        else:
            raise ValueError(f"invalid schedule day {item!r}")
    if not out:
        raise ValueError("schedule days must not be empty")
    return frozenset(out)


def parse_schedule(raw: Optional[Sequence[Mapping[str, Any]]], default_timezone: Optional[str] = None) -> tuple:
    return tuple(ScheduleWindow.parse(w, default_timezone) for w in (raw or ()))


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class GovernancePolicy:
    """Owner-visible rules. Every default is the conservative one (X13)."""

    yield_to_owner: bool = True  # foreground GPU app and recent input stop new claims. Full-screen is ALWAYS refused.
    idle_threshold_seconds: float = 300.0  # input newer than this means the owner is there
    resume_clear_seconds: float = 60.0  # hysteresis: every cause clear this long before going back online
    allow_on_battery: bool = False
    vram_reserve_mb: int = 1024  # VRAM kept free for the owner; a claim must fit in (free - reserve)
    schedule: tuple = ()  # empty = no schedule limit; otherwise only inside a window
    ignored_processes: frozenset = DEFAULT_IGNORED_PROCESSES
    stop_now_hold_seconds: float = 0.0  # 0 = a "stop now" lasts until resume()
    preempt_on_fullscreen: bool = False  # True stops a RUNNING job when a full-screen app appears (owner decision)
    unknown_blocks: tuple = UNKNOWN_BLOCKS_DEFAULT

    def __post_init__(self) -> None:
        if self.idle_threshold_seconds < 0 or self.resume_clear_seconds < 0 or self.stop_now_hold_seconds < 0:
            raise ValueError("governance times must be >= 0")
        if self.vram_reserve_mb < 0:
            raise ValueError("vram_reserve_mb must be >= 0")

    @classmethod
    def headless(cls, **overrides: Any) -> "GovernancePolicy":
        """A host with no desktop session (datacenter / server): activity signals are not required to be known.
        Battery, reboot, sleep, schedule, VRAM reserve and ``stop now`` still apply."""
        base = {"yield_to_owner": False, "unknown_blocks": ()}
        base.update(overrides)
        return cls(**base)

    @classmethod
    def from_config(cls, config: Any) -> "GovernancePolicy":
        """Build from the additive ``governance_*`` AgentConfig fields (missing fields keep the conservative default)."""
        def get(name: str, default: Any) -> Any:
            return getattr(config, name, default)

        extra = {str(p).strip().lower() for p in (get("governance_ignored_processes", []) or []) if str(p).strip()}
        headless = bool(get("governance_headless", False))
        return cls(
            yield_to_owner=bool(get("governance_yield_to_owner", True)) and not headless,
            idle_threshold_seconds=float(get("governance_idle_threshold_seconds", 300)),
            resume_clear_seconds=float(get("governance_resume_clear_seconds", 60)),
            allow_on_battery=bool(get("governance_allow_on_battery", False)),
            vram_reserve_mb=int(get("governance_vram_reserve_mb", 1024)),
            schedule=parse_schedule(get("governance_schedule", []) or []),
            ignored_processes=frozenset(DEFAULT_IGNORED_PROCESSES | extra),
            stop_now_hold_seconds=float(get("governance_stop_now_hold_minutes", 0)) * 60.0,
            preempt_on_fullscreen=bool(get("governance_preempt_on_fullscreen", False)),
            unknown_blocks=() if headless else UNKNOWN_BLOCKS_DEFAULT,
        )


# --------------------------------------------------------------------------- #
# Signals and decisions
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Signals:
    """One observation. ``None`` means UNKNOWN (the probe could not tell), never "false"."""

    now: dt.datetime
    owner_active: Optional[bool] = None  # PAL ActivitySnapshot.owner_active (recent input or GPU app), if the probe has it
    fullscreen: Optional[bool] = None
    idle_seconds: Optional[float] = None
    foreground_gpu_app: Optional[str] = None  # name of the foreground app that is using the GPU, None = none seen
    on_battery: Optional[bool] = None
    sleep_pending: Optional[bool] = None  # None = no suspend event seen
    reboot_pending: Optional[bool] = None
    free_vram_mb: Optional[int] = None  # unknown never blocks on its own: the VRAM check needs a number to compare
    job_running: bool = False
    probe_errors: tuple = ()  # names of probes that RAISED (as opposed to answering unknown); always blocks


@dataclasses.dataclass(frozen=True)
class Decision:
    state: State
    code: str  # stable machine-readable primary cause: "ok", "fullscreen", "foreground_gpu_app", "reboot_pending", ...
    reason: str  # one human line for the tray, the console and logs
    causes: tuple = ()  # every active cause code, highest priority first
    stop_job: bool = False  # the caller must stop the running job now (only an explicit "stop now" or a preempting policy)
    prevent_sleep: bool = False  # the caller holds a sleep-prevention request (only while a job runs)
    since: Optional[dt.datetime] = None  # when this state began

    @property
    def claim_allowed(self) -> bool:
        return self.state is State.ONLINE

    @property
    def heartbeat_availability(self) -> str:
        return HEARTBEAT_AVAILABILITY[self.state]

    def heartbeat_health(self) -> dict:
        """Additive health fields for the heartbeat (the API drops unknown ones until it learns them)."""
        return {"availability_detail": {"state": self.state.value, "code": self.code}}


def _name_of(app: Optional[str]) -> str:
    return (app or "").strip().lower()


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class GovernanceEngine:
    """Thread-safe: ``evaluate`` is called from the claim loop and the heartbeat, ``stop_now`` from the tray or the CLI."""

    def __init__(self, policy: Optional[GovernancePolicy] = None) -> None:
        self.policy = policy or GovernancePolicy()
        self._lock = threading.Lock()
        # Start closed: the first evaluation must see every cause clear for ``resume_clear_seconds`` (fail closed, L1).
        self._state = State.YIELDING
        self._kind = "yield"
        self._since: Optional[dt.datetime] = None
        self._clear_since: Optional[dt.datetime] = None
        self._stop_now = False
        self._stop_now_until: Optional[dt.datetime] = None
        self._last: Optional[Decision] = None

    # ---- owner overrides ------------------------------------------------- #
    def stop_now(self, now: dt.datetime) -> None:
        """The owner pressed "stop now": stop the running job and claim nothing until :meth:`resume`
        (or ``stop_now_hold_seconds``, when set)."""
        with self._lock:
            self._stop_now = True
            hold = self.policy.stop_now_hold_seconds
            self._stop_now_until = now + dt.timedelta(seconds=hold) if hold > 0 else None

    def resume(self) -> None:
        """Lift ``stop now``. Going back online still needs the normal clear period."""
        with self._lock:
            self._stop_now = False
            self._stop_now_until = None

    @property
    def stopped_by_owner(self) -> bool:
        with self._lock:
            return self._stop_now

    # ---- evaluation ------------------------------------------------------ #
    def evaluate(self, s: Signals) -> Decision:
        if s.now.tzinfo is None:
            raise ValueError("Signals.now must be timezone-aware")
        with self._lock:
            return self._evaluate_locked(s)

    def last(self) -> Optional[Decision]:
        with self._lock:
            return self._last

    def _causes(self, s: Signals) -> list:
        """(kind, code, reason) in priority order. kind: 'stop' | 'maintenance' | 'yield'."""
        p = self.policy
        out: list = []
        if self._stop_now and self._stop_now_until is not None and s.now >= self._stop_now_until:
            self._stop_now, self._stop_now_until = False, None
        if self._stop_now:
            out.append(("stop", "owner_stop_now", "stopped by the owner (stop now)"))
        if s.reboot_pending is True:
            out.append(("maintenance", "reboot_pending", "a Windows Update restart is pending"))
        if s.sleep_pending is True:
            out.append(("maintenance", "sleep_pending", "the computer is going to sleep or hibernate"))
        if s.probe_errors:
            out.append(("yield", "probe_error", "a status check failed: " + ", ".join(sorted(s.probe_errors))))
        if s.fullscreen is True:
            out.append(("yield", "fullscreen", "a full-screen app is running"))
        elif s.fullscreen is None and "fullscreen" in p.unknown_blocks:
            out.append(("yield", "unknown_fullscreen", "cannot tell whether a full-screen app is running"))
        if p.yield_to_owner:
            app = _name_of(s.foreground_gpu_app)
            if app and app not in p.ignored_processes:
                out.append(("yield", "foreground_gpu_app", f"the owner is using the GPU ({app})"))
            if s.owner_active is True:
                out.append(("yield", "owner_active", "the owner is using the computer"))
            if s.idle_seconds is not None:
                if s.idle_seconds < p.idle_threshold_seconds:
                    out.append(("yield", "recent_input", f"keyboard or mouse used {int(s.idle_seconds)}s ago"))
            elif s.owner_active is None and "idle" in p.unknown_blocks:
                out.append(("yield", "unknown_idle", "cannot tell whether the owner is at the computer"))
        if s.on_battery is True and not p.allow_on_battery:
            out.append(("yield", "on_battery", "running on battery"))
        elif s.on_battery is None and "battery" in p.unknown_blocks and not p.allow_on_battery:
            out.append(("yield", "unknown_battery", "cannot tell whether the computer is on battery"))
        if s.reboot_pending is None and "reboot" in p.unknown_blocks:
            out.append(("maintenance", "unknown_reboot", "cannot tell whether a restart is pending"))
        if p.schedule and not any(w.contains(s.now) for w in p.schedule):
            out.append(("yield", "outside_schedule", "outside the hours the owner allowed"))
        if not s.job_running and s.free_vram_mb is not None and s.free_vram_mb < p.vram_reserve_mb:
            # Only without a job of ours: our own job's memory would otherwise make us yield to ourselves.
            out.append(("yield", "vram_reserve", f"only {s.free_vram_mb} MB of GPU memory is free"))
        return out

    def _evaluate_locked(self, s: Signals) -> Decision:
        p = self.policy
        causes = self._causes(s)
        kinds = {c[0] for c in causes}
        primary = causes[0] if causes else None
        stop_job = False

        if primary is None:
            raw_state, raw_kind = State.ONLINE, "ok"
        elif "stop" in kinds:
            raw_state, raw_kind, stop_job = State.YIELDING, "yield", s.job_running
        else:
            raw_kind = "maintenance" if primary[0] == "maintenance" else "yield"
            if s.job_running:
                raw_state = State.DRAINING
                if p.preempt_on_fullscreen and s.fullscreen is True:
                    stop_job = True
            else:
                raw_state = State.MAINTENANCE if raw_kind == "maintenance" else State.YIELDING

        if raw_state is not State.ONLINE:
            self._clear_since = None
            new_state, code, reason = raw_state, primary[1], primary[2]
            new_kind = raw_kind
        else:
            new_state, code, reason, new_kind = self._settle(s)

        if new_state is not self._state or self._since is None:
            self._since = s.now
        self._state, self._kind = new_state, new_kind
        decision = Decision(
            state=new_state,
            code=code,
            reason=reason,
            causes=tuple(c[1] for c in causes),
            stop_job=stop_job,
            prevent_sleep=bool(s.job_running and not stop_job and s.sleep_pending is not True),
            since=self._since,
        )
        self._last = decision
        return decision

    def _settle(self, s: Signals) -> tuple:
        """Every cause is clear. Go online only after ``resume_clear_seconds`` of unbroken calm."""
        if self._state is State.ONLINE:
            return State.ONLINE, "ok", "available", "ok"
        if self._clear_since is None or s.now < self._clear_since:  # first clear sample, or the clock went backwards
            self._clear_since = s.now
        waited = (s.now - self._clear_since).total_seconds()
        needed = self.policy.resume_clear_seconds
        if waited >= needed:
            self._clear_since = None
            return State.ONLINE, "ok", "available", "ok"
        if self._state is State.DRAINING and not s.job_running:
            held = State.MAINTENANCE if self._kind == "maintenance" else State.YIELDING
        elif self._state is State.DRAINING:
            held = State.DRAINING
        else:
            held = self._state
        return held, "settling", f"waiting {int(needed - waited) + 1}s to be sure it is quiet", self._kind

    # ---- claim-time helper ----------------------------------------------- #
    def fits_vram(self, free_vram_mb: Optional[int], required_mb: int) -> bool:
        """True when ``required_mb`` fits while keeping the owner's reserve. Unknown free memory is refused (L1)."""
        if free_vram_mb is None or required_mb < 0:
            return False
        return free_vram_mb - self.policy.vram_reserve_mb >= required_mb


# --------------------------------------------------------------------------- #
# Collecting signals from injected probes
# --------------------------------------------------------------------------- #
class ProbeUnsupported(Exception):
    """A probe raises this for a signal it has no way to read; the signal is then UNKNOWN (None), not an error."""


def collect_signals(
    now: dt.datetime,
    *,
    activity: Any = None,
    power: Any = None,
    owner_gpu: Any = None,
    job_running: bool = False,
) -> Signals:
    """Build :class:`Signals` from injected probes.

    * ``activity``: a PAL ``ActivityProbe`` (``snapshot() -> ActivitySnapshot``)
    * ``power``: a PAL ``PowerState`` (``snapshot() -> PowerSnapshot``)
    * ``owner_gpu``: anything with ``foreground_gpu_app() -> Optional[str]`` and ``free_vram_mb() -> Optional[int]``

    A probe that raises ``PlatformUnsupported`` or :class:`ProbeUnsupported` yields UNKNOWN signals; any other exception
    is recorded in ``probe_errors`` (fail closed).
    """
    errors: list = []

    def call(name: str, fn: Optional[Callable[[], Any]]) -> Any:
        if fn is None:
            return None
        try:
            return fn()
        except ProbeUnsupported:
            return None
        except Exception as exc:  # noqa: BLE001 - a probe must never stop the agent; it blocks claims instead
            if type(exc).__name__ == "PlatformUnsupported":
                return None
            errors.append(name)
            return None

    act = call("activity", getattr(activity, "snapshot", None))
    pwr = call("power", getattr(power, "snapshot", None))
    return Signals(
        now=now,
        owner_active=getattr(act, "owner_active", None),
        fullscreen=getattr(act, "fullscreen_app", None),
        idle_seconds=getattr(act, "idle_seconds", None),
        foreground_gpu_app=call("foreground_gpu_app", getattr(owner_gpu, "foreground_gpu_app", None)),
        free_vram_mb=call("free_vram", getattr(owner_gpu, "free_vram_mb", None)),
        on_battery=getattr(pwr, "on_battery", None),
        sleep_pending=getattr(pwr, "sleep_pending", None),
        reboot_pending=getattr(pwr, "reboot_pending", None),
        job_running=job_running,
        probe_errors=tuple(errors),
    )
