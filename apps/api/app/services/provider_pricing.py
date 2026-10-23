"""Provider-owned market policy: price floor, availability windows, model allowlist.

WHY this module exists
----------------------
Phase C3 gives operators hard controls over *when*, *for what* and *at what
price* their hardware may be scheduled. The scheduler must not re-derive these
rules ad hoc, so every rule lives here as a pure, side-effect-free function and
the scheduler calls :func:`is_machine_eligible_now`.

Three controls (all stored on ``provider_machines``):

* ``floor_price_per_hour`` — a HARD floor. The marketplace must never quote or
  schedule the machine below it. A machine whose asking price has drifted below
  its own floor is treated as INELIGIBLE rather than silently sold too cheap:
  refusing work is recoverable, underpaying the operator is not.
* ``availability_windows`` — recurring local-time windows the operator accepts
  work in (e.g. overnight only). Empty/NULL means "always available".
* ``model_allowlist`` — model version tags the operator is willing to run.
  Empty/NULL means "any approved model".

All money here is :class:`decimal.Decimal` — never float. All datetime
comparisons go through :func:`app.db.base.ensure_aware` because SQLite drops
tzinfo on round-trip.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db.base import ensure_aware, utcnow
from app.models.provider import ProviderMachine

__all__ = [
    "PricingPolicyError",
    "MAX_WINDOWS",
    "MAX_ALLOWLIST_ENTRIES",
    "validate_floor_price",
    "validate_availability_windows",
    "validate_model_allowlist",
    "is_within_availability_window",
    "is_model_allowed",
    "price_respects_floor",
    "machine_ineligibility_reason",
    "is_machine_eligible_now",
]

MAX_WINDOWS = 32
MAX_ALLOWLIST_ENTRIES = 100
_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DAY = dt.timedelta(days=1)


class PricingPolicyError(ValueError):
    """Raised when operator-supplied pricing policy is malformed.

    Carries a stable machine-readable ``code`` so the router can surface it as a
    422 detail without leaking internals.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


# ---------------------------------------------------------------------------
# Validation / normalization
# ---------------------------------------------------------------------------
def validate_floor_price(value: decimal.Decimal | int | str | None) -> decimal.Decimal | None:
    """Return the floor price quantized to 2dp, or ``None`` to clear it.

    A floor of zero or less is meaningless (it would not floor anything) and a
    non-numeric value is a client bug, so both are refused rather than coerced.
    """
    if value is None:
        return None
    try:
        price = decimal.Decimal(str(value))
    except (decimal.InvalidOperation, ValueError):
        raise PricingPolicyError("floor_price_not_a_number")
    if not price.is_finite():
        raise PricingPolicyError("floor_price_not_a_number")
    if price <= 0:
        raise PricingPolicyError("floor_price_must_be_positive")
    if price > decimal.Decimal("1000000"):
        raise PricingPolicyError("floor_price_too_large")
    return price.quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)


def _parse_hhmm(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not _HHMM.match(raw):
        raise PricingPolicyError(f"window_bad_{field}")
    return raw


def _parse_days(raw: Any) -> list[int]:
    """Normalize the day-of-week list (0=Monday .. 6=Sunday); absent == every day."""
    if raw is None:
        return list(range(7))
    if not isinstance(raw, (list, tuple)) or not raw:
        raise PricingPolicyError("window_bad_days")
    days: list[int] = []
    for day in raw:
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise PricingPolicyError("window_bad_days")
        if day not in days:
            days.append(day)
    return sorted(days)


def _parse_tz(raw: Any) -> str:
    if raw is None:
        return "UTC"
    if not isinstance(raw, str) or not raw:
        raise PricingPolicyError("window_bad_tz")
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise PricingPolicyError("window_bad_tz")
    return raw


def validate_availability_windows(windows: Any) -> list[dict] | None:
    """Validate and normalize an availability schedule.

    Accepted shape (per entry)::

        {"days": [0,1,2,3,4], "start": "18:00", "end": "06:00", "tz": "UTC"}

    ``end <= start`` means the window wraps past midnight (an overnight shift),
    which is the common case for "my desktop is free while I sleep". ``None`` or
    ``[]`` clears the schedule, restoring "always available".
    """
    if windows is None:
        return None
    if not isinstance(windows, (list, tuple)):
        raise PricingPolicyError("windows_not_a_list")
    if len(windows) > MAX_WINDOWS:
        raise PricingPolicyError("too_many_windows")
    normalized: list[dict] = []
    for entry in windows:
        if not isinstance(entry, dict):
            raise PricingPolicyError("window_not_an_object")
        start = _parse_hhmm(entry.get("start"), "start")
        end = _parse_hhmm(entry.get("end"), "end")
        if start == end:
            # Ambiguous: zero-length or 24h? Refuse rather than guess.
            raise PricingPolicyError("window_zero_length")
        normalized.append(
            {"days": _parse_days(entry.get("days")), "start": start, "end": end, "tz": _parse_tz(entry.get("tz"))}
        )
    return normalized


def validate_model_allowlist(models: Any) -> list[str] | None:
    """Validate a model version-tag allowlist. ``None``/``[]`` means "any approved"."""
    if models is None:
        return None
    if not isinstance(models, (list, tuple)):
        raise PricingPolicyError("allowlist_not_a_list")
    if len(models) > MAX_ALLOWLIST_ENTRIES:
        raise PricingPolicyError("allowlist_too_large")
    out: list[str] = []
    for tag in models:
        if not isinstance(tag, str):
            raise PricingPolicyError("allowlist_entry_not_a_string")
        tag = tag.strip()
        if not tag or len(tag) > 128:
            raise PricingPolicyError("allowlist_entry_invalid")
        if tag not in out:
            out.append(tag)
    return out


# ---------------------------------------------------------------------------
# Rule evaluation (pure)
# ---------------------------------------------------------------------------
def _window_contains(window: dict, now: dt.datetime) -> bool:
    """True when ``now`` (tz-aware) falls inside one recurrence of ``window``."""
    tz = ZoneInfo(window.get("tz") or "UTC")
    local = now.astimezone(tz)
    start_h, start_m = (int(p) for p in window["start"].split(":"))
    end_h, end_m = (int(p) for p in window["end"].split(":"))
    start_delta = dt.timedelta(hours=start_h, minutes=start_m)
    end_delta = dt.timedelta(hours=end_h, minutes=end_m)
    # end <= start -> the window wraps past midnight.
    duration = end_delta - start_delta
    if duration <= dt.timedelta(0):
        duration += _DAY
    days = window.get("days") or list(range(7))

    # Check today's recurrence and yesterday's (a wrapping window started then).
    for offset in (0, -1):
        day_date = (local + dt.timedelta(days=offset)).date()
        if day_date.weekday() not in days:
            continue
        start_at = dt.datetime.combine(day_date, dt.time(start_h, start_m), tzinfo=tz)
        if start_at <= local < start_at + duration:
            return True
    return False


def is_within_availability_window(
    windows: Iterable[dict] | None, now: dt.datetime | None = None
) -> bool:
    """True when the schedule is empty (always on) or ``now`` is inside a window."""
    if not windows:
        return True
    moment = ensure_aware(now) or utcnow()
    return any(_window_contains(w, moment) for w in windows)


def is_model_allowed(allowlist: Iterable[str] | None, model: str | None) -> bool:
    """True when the allowlist is empty (any approved model) or contains ``model``."""
    entries = list(allowlist or [])
    if not entries:
        return True
    if model is None:
        # A machine that restricts models cannot serve an unspecified model.
        return False
    return model in entries


def price_respects_floor(
    price: decimal.Decimal | int | str | None, floor: decimal.Decimal | int | str | None
) -> bool:
    """True when ``price`` is at or above ``floor`` (no floor == always true)."""
    if floor is None:
        return True
    if price is None:
        return False
    return decimal.Decimal(str(price)) >= decimal.Decimal(str(floor))


def machine_ineligibility_reason(
    machine: ProviderMachine, model: str | None = None, now: dt.datetime | None = None
) -> str | None:
    """Return the first policy reason the machine may not take work, else ``None``.

    Kept separate from :func:`is_machine_eligible_now` so the scheduler can log a
    precise reason without re-running the checks.
    """
    moment = ensure_aware(now) or utcnow()
    if machine.status == "deregistered":
        return "deregistered"
    if not price_respects_floor(machine.asking_price_per_hour, machine.floor_price_per_hour):
        return "price_below_floor"
    if not is_within_availability_window(machine.availability_windows, moment):
        return "outside_availability_window"
    if not is_model_allowed(machine.model_allowlist, model):
        return "model_not_allowlisted"
    return None


def is_machine_eligible_now(
    machine: ProviderMachine, model: str | None = None, now: dt.datetime | None = None
) -> bool:
    """Operator-policy gate for the scheduler.

    This is ONLY the operator-policy layer (floor price, schedule, model
    allowlist). Hardware fit, workload support, heartbeat freshness and capacity
    remain the scheduler's own checks — this function never overrides them.
    """
    return machine_ineligibility_reason(machine, model, now) is None
