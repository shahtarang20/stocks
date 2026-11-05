"""GPU thermal capping (B6).

* at or above ``cap_c``: the agent stops claiming new jobs and reports availability
  ``"throttled"``; it resumes only below ``resume_c`` (hysteresis, so it does not flap);
* at or above ``hard_stop_c``: the running container is stopped and the job reported failed with
  ``thermal_shutdown`` (the platform does not charge a failed job and it can fail over);
* a missing reading (None, no sensor / driver error) NEVER triggers anything and NEVER blocks:
  an unreadable sensor clears a throttle rather than holding the machine out forever.

Readings come from the existing telemetry (``sample_gpu_metrics``: NVML for NVIDIA, amdsmi for AMD).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

OK = "ok"
THROTTLED = "throttled"
HARD_STOP = "hard_stop"


def _default_sampler() -> list[Any]:
    from .telemetry import sample_gpu_metrics

    return sample_gpu_metrics(simulated=False)


class ThermalGuard:
    def __init__(
        self,
        cap_c: float = 85.0,
        resume_c: float = 80.0,
        hard_stop_c: float = 95.0,
        *,
        sampler: Callable[[], list[Any]] = _default_sampler,
    ) -> None:
        if not (resume_c < cap_c < hard_stop_c):
            raise ValueError("thermal limits must satisfy resume < cap < hard_stop")
        self.cap_c, self.resume_c, self.hard_stop_c = cap_c, resume_c, hard_stop_c
        self._sampler = sampler
        self._lock = threading.Lock()
        self._throttled = False

    @classmethod
    def from_config(cls, config: Any, *, sampler: Optional[Callable[[], list[Any]]] = None) -> "ThermalGuard":
        kwargs: dict[str, Any] = {}
        if sampler is not None:
            kwargs["sampler"] = sampler
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
                self._throttled = False
                return OK
            if temp_c >= self.hard_stop_c:
                self._throttled = True
                return HARD_STOP
            if temp_c >= self.cap_c:
                self._throttled = True
            elif temp_c < self.resume_c:
                self._throttled = False
            return THROTTLED if self._throttled else OK

    def check(self) -> str:
        return self.evaluate(self.read_max_temp())

    def availability(self) -> str:
        """``"throttled"`` or ``"online"`` for the heartbeat (re-reads the sensor)."""
        return "throttled" if self.check() in (THROTTLED, HARD_STOP) else "online"

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
