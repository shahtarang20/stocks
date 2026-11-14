"""GPU thermal capping (B6).

* at or above ``cap_c``: the agent stops claiming new jobs and reports availability
  ``"throttled"``; it resumes only below ``resume_c`` (hysteresis, so it does not flap);
* at or above ``hard_stop_c``: the running container is stopped and the job reported failed with
  ``thermal_shutdown`` (the platform does not charge a failed job and it can fail over);
* a computer that has NEVER given a temperature (no sensor, no driver support) cannot be protected by this guard: it is
  left alone, exactly as before, and the agent says so once at start;
* a computer that DID give a temperature and then stops (driver crash, sensor error) is flying blind. A short gap is
  tolerated and keeps the previous state; after ``sensor_lost_seconds`` without a single reading the guard reports
  ``sensor_lost``: no new jobs, and a running job is stopped (``thermal_shutdown``: the host's doing, never charged).
  Any real reading ends it. A reading that stays the SAME is deliberately NOT treated as a fault: whole-degree sensors
  legitimately report the same number for hours.

Readings come from the existing telemetry (``sample_gpu_metrics``: NVML for NVIDIA, amdsmi for AMD).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

OK = "ok"
THROTTLED = "throttled"
HARD_STOP = "hard_stop"
SENSOR_LOST = "sensor_lost"

#: The states in which a computer must not take new work (and, for HARD_STOP / SENSOR_LOST, must stop what it is running).
BLOCKING_STATES = (THROTTLED, HARD_STOP, SENSOR_LOST)
DEFAULT_SENSOR_LOST_SECONDS = 120.0


SIMULATED_MODE = "simulated_probe"  # telemetry.sample_gpu_metrics' fake sample (48.5 C) when no GPU can be read


def real_samples_only(samples: Any) -> list[Any]:
    """Drop the fake sample ``sample_gpu_metrics`` returns when nothing is readable. A fake "cool" reading would hide a
    driver that has stopped answering, and would make a computer with no GPU look as if it had a working sensor."""
    return [s for s in (samples or []) if (getattr(s, "extra_metrics", None) or {}).get("mode") != SIMULATED_MODE]


def _default_sampler() -> list[Any]:
    from .telemetry import sample_gpu_metrics

    return real_samples_only(sample_gpu_metrics(simulated=False))


class ThermalGuard:
    def __init__(
        self,
        cap_c: float = 85.0,
        resume_c: float = 80.0,
        hard_stop_c: float = 95.0,
        *,
        sampler: Callable[[], list[Any]] = _default_sampler,
        sensor_lost_seconds: float = DEFAULT_SENSOR_LOST_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not (resume_c < cap_c < hard_stop_c):
            raise ValueError("thermal limits must satisfy resume < cap < hard_stop")
        if not sensor_lost_seconds > 0:
            raise ValueError("sensor_lost_seconds must be positive")
        self.cap_c, self.resume_c, self.hard_stop_c = cap_c, resume_c, hard_stop_c
        self.sensor_lost_seconds = float(sensor_lost_seconds)
        self._sampler = sampler
        self._clock = clock
        self._lock = threading.Lock()
        self._throttled = False
        self._ever_read = False
        self._blind_since: Optional[float] = None
        self._warned_blind = False

    @classmethod
    def from_config(cls, config: Any, *, sampler: Optional[Callable[[], list[Any]]] = None) -> "ThermalGuard":
        kwargs: dict[str, Any] = {}
        if sampler is not None:
            kwargs["sampler"] = sampler
        lost = getattr(config, "gpu_sensor_lost_seconds", None)
        if isinstance(lost, (int, float)) and not isinstance(lost, bool) and lost > 0:
            kwargs["sensor_lost_seconds"] = float(lost)
        return cls(config.gpu_temp_cap_c, config.gpu_temp_resume_c, config.gpu_temp_hard_stop_c, **kwargs)

    def read_max_temp(self) -> Optional[float]:
        """Hottest GPU reading, or None when there is none (never raises)."""
        try:
            temps = [s.temp_c for s in (self._sampler() or []) if getattr(s, "temp_c", None) is not None]
        except Exception as exc:  # a broken sensor must not stop the agent
            logger.debug("thermal sample failed: %s", exc)
            return None
        return max(temps) if temps else None

    def evaluate(self, temp_c: Optional[float]) -> str:
        with self._lock:
            if temp_c is None:
                if not self._ever_read:
                    # No sensor at all: nothing to protect with, and nothing to hold the machine out for.
                    self._throttled = False
                    return OK
                now = self._clock()
                if self._blind_since is None:
                    self._blind_since = now
                if now - self._blind_since >= self.sensor_lost_seconds:
                    if not self._warned_blind:
                        self._warned_blind = True
                        logger.warning(
                            "GPU temperature unreadable for %.0f s: taking no new jobs and stopping a running one until "
                            "a reading comes back", now - self._blind_since,
                        )
                    return SENSOR_LOST
                # A short gap: keep whatever was true a moment ago (never clear a throttle on a blip).
                return THROTTLED if self._throttled else OK
            self._ever_read = True
            self._blind_since = None
            self._warned_blind = False
            if temp_c >= self.hard_stop_c:
                self._throttled = True
                return HARD_STOP
            if temp_c >= self.cap_c:
                self._throttled = True
            elif temp_c < self.resume_c:
                self._throttled = False
            return THROTTLED if self._throttled else OK

    def blind_seconds(self) -> float:
        """How long the sensor has been unreadable (0 when it is readable or has never worked)."""
        with self._lock:
            return 0.0 if self._blind_since is None else max(0.0, self._clock() - self._blind_since)

    def has_sensor(self) -> bool:
        """True once a real temperature has been read at least once."""
        with self._lock:
            return self._ever_read

    def check(self) -> str:
        return self.evaluate(self.read_max_temp())

    def availability(self) -> str:
        """``"throttled"`` or ``"online"`` for the heartbeat (re-reads the sensor)."""
        return "throttled" if self.check() in BLOCKING_STATES else "online"

    def may_claim(self) -> bool:
        return self.check() == OK


def apply_power_limit(
    limit_w: Optional[float],
    *,
    nvml_module: Any = None,
    amdsmi_module: Any = None,
) -> bool:
    """Set the board power limit when configured AND permitted. Never raises, never fails enrolment.

    NVIDIA: ``nvmlDeviceSetPowerManagementLimit(handle, milliwatts)`` (needs root/CAP_SYS_ADMIN).
    AMD: ``amdsmi_set_power_cap(handle, 0, cap_microwatts)`` (needs root).
    Returns True when at least one GPU was set; on any refusal logs and returns False.
    NOTE (L4): confirm both call signatures against the vendor docs on a real host before relying on this.
    """
    if limit_w is None:
        return False
    if limit_w <= 0:
        logger.warning("gpu_power_limit_w=%s ignored (must be positive)", limit_w)
        return False
    done = False
    try:
        nvml = nvml_module
        if nvml is None:
            try:
                import pynvml as nvml  # type: ignore
            except Exception:
                nvml = None
        if nvml is not None:
            try:
                nvml.nvmlInit()
                for i in range(nvml.nvmlDeviceGetCount()):
                    nvml.nvmlDeviceSetPowerManagementLimit(nvml.nvmlDeviceGetHandleByIndex(i), int(limit_w * 1000))
                    done = True
            except Exception as exc:
                logger.warning("could not set NVIDIA power limit (not permitted?): %s", exc)
            finally:
                try:
                    nvml.nvmlShutdown()
                except Exception:
                    pass
        amd = amdsmi_module
        if amd is None:
            try:
                import amdsmi as amd  # type: ignore
            except Exception:
                amd = None
        if amd is not None:
            try:
                amd.amdsmi_init()
                for handle in amd.amdsmi_get_processor_handles():
                    amd.amdsmi_set_power_cap(handle, 0, int(limit_w * 1_000_000))
                    done = True
            except Exception as exc:
                logger.warning("could not set AMD power limit (not permitted?): %s", exc)
            finally:
                try:
                    amd.amdsmi_shut_down()
                except Exception:
                    pass
    except Exception as exc:  # belt and braces
        logger.warning("power limit failed: %s", exc)
    return done


# --------------------------------------------------------------------------- #
# Default power cap: a percentage of each card's OWN default limit, put back when the agent stops
# --------------------------------------------------------------------------- #
DEFAULT_POWER_PERCENT = 90.0
MIN_POWER_PERCENT = 50.0


@dataclass
class PowerCapResult:
    """What was changed, so it can be put back. ``changed`` maps GPU index to ``(old_milliwatts, new_milliwatts)``."""

    changed: dict = field(default_factory=dict)
    reason: str = ""  # why nothing was changed, when nothing was


def apply_power_percent(percent: Optional[float], *, nvml_module: Any = None) -> PowerCapResult:
    """Lower each NVIDIA card's power limit to ``percent`` of ITS OWN default limit, when the agent is allowed to.

    * a limit the owner already set LOWER than the target is left alone (the agent never raises a limit);
    * the target is kept inside the card's allowed range;
    * 100 (or more) changes nothing;
    * AMD cards are not touched here: ``amdsmi``'s cap calls have not been confirmed against a real card (L4), so an AMD
      host keeps its card's own limit unless the owner sets ``gpu_power_limit_w``.

    Never raises. The caller keeps the result and calls :func:`restore_power_limits` when the agent stops."""
    if percent is None or not (MIN_POWER_PERCENT <= percent < 100.0):
        return PowerCapResult(reason="off" if percent is None or percent >= 100.0 else "percent out of range")
    nvml = nvml_module
    if nvml is None:
        try:
            import pynvml as nvml  # type: ignore
        except Exception:
            return PowerCapResult(reason="no NVIDIA library")
    result = PowerCapResult()
    try:
        nvml.nvmlInit()
    except Exception as exc:
        return PowerCapResult(reason=f"NVML unavailable: {type(exc).__name__}")
    try:
        for i in range(nvml.nvmlDeviceGetCount()):
            try:
                handle = nvml.nvmlDeviceGetHandleByIndex(i)
                default_mw = int(nvml.nvmlDeviceGetPowerManagementDefaultLimit(handle))
                current_mw = int(nvml.nvmlDeviceGetPowerManagementLimit(handle))
                low_mw, high_mw = nvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
                target_mw = max(int(low_mw), min(int(high_mw), int(default_mw * percent / 100.0)))
                if target_mw >= current_mw:
                    continue  # already at or below the target (possibly set by the owner): leave it
                nvml.nvmlDeviceSetPowerManagementLimit(handle, target_mw)
                result.changed[i] = (current_mw, target_mw)
            except Exception as exc:  # one card refusing (no permission, unsupported) never blocks the others
                logger.warning("could not cap power on GPU %s: %s", i, type(exc).__name__)
                result.reason = result.reason or f"not permitted: {type(exc).__name__}"
    except Exception as exc:
        result.reason = result.reason or type(exc).__name__
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass
    return result


def restore_power_limits(result: Optional[PowerCapResult], *, nvml_module: Any = None) -> int:
    """Put every changed card back to the limit it had before. Returns how many were restored. Never raises.

    A card is put back only if it is still at the limit the agent set. If someone else changed it since (the owner, a
    reboot, another tool), their value is left alone."""
    if result is None or not result.changed:
        return 0
    nvml = nvml_module
    if nvml is None:
        try:
            import pynvml as nvml  # type: ignore
        except Exception:
            return 0
    restored = 0
    try:
        nvml.nvmlInit()
    except Exception:
        return 0
    try:
        for i, (old_mw, new_mw) in result.changed.items():
            try:
                handle = nvml.nvmlDeviceGetHandleByIndex(i)
                if int(nvml.nvmlDeviceGetPowerManagementLimit(handle)) != int(new_mw):
                    continue  # not our cap any more: leave it
                nvml.nvmlDeviceSetPowerManagementLimit(handle, int(old_mw))
                restored += 1
            except Exception as exc:
                logger.warning("could not restore the power limit on GPU %s: %s", i, type(exc).__name__)
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass
    return restored


def power_cap_to_json(result: PowerCapResult) -> str:
    """For the small state file that lets a LATER run undo a cap this run could not (a crash, a power cut)."""
    import json

    return json.dumps({str(i): [int(o), int(n)] for i, (o, n) in result.changed.items()})


def power_cap_from_json(text: str) -> PowerCapResult:
    """The inverse. Anything unreadable gives an empty result (nothing to undo), never an error."""
    import json

    try:
        raw = json.loads(text)
        changed = {int(i): (int(v[0]), int(v[1])) for i, v in raw.items()}
    except Exception:
        return PowerCapResult()
    return PowerCapResult(changed=changed)


def undo_leftover_power_cap(state_file: Any, emit: Optional[Callable[[str], None]] = None) -> int:
    """Put back the limits a run that never got to restore left behind (read from ``state_file``), then forget them.

    Only cards still at the cap that run set are touched (see :func:`restore_power_limits`). Never raises; returns how many
    cards were restored."""
    n = 0
    try:
        leftover = power_cap_from_json(state_file.read_text()) if state_file.exists() else None
    except OSError:
        leftover = None
    if leftover is not None and leftover.changed:
        n = restore_power_limits(leftover)
        if emit:
            emit(f"[power] put back the power limit an earlier run left on {n} card(s)")
    try:
        state_file.unlink()
    except OSError:
        pass
    return n


def record_power_cap(state_file: Any, result: PowerCapResult) -> bool:
    """Write what was changed, so a later run can undo it if this one cannot. True when it was written."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(power_cap_to_json(result))
        return True
    except OSError:
        return False


def restore_recorded_power_cap(state_file: Any) -> int:
    """Undo the cap recorded in ``state_file`` (used by the separate power-cap service when it stops). Never raises."""
    try:
        recorded = power_cap_from_json(state_file.read_text()) if state_file.exists() else None
    except OSError:
        recorded = None
    n = restore_power_limits(recorded) if recorded is not None and recorded.changed else 0
    try:
        state_file.unlink()
    except OSError:
        pass
    return n
