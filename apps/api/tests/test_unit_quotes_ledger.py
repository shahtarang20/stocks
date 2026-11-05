"""U15: a unit quote never lets a job exceed its cap, and billing stays metered GPU-seconds. WRITTEN, NOT YET RUN.

Uses only the existing, unmodified pure billing function ``_meter_actual_cost`` (what ``settle_job`` calls) and
the cap rule ``settle_job`` applies (capture = min(actual, held)); no ledger writes. The unit quote is
display + cap only: nothing in it changes how the usage is metered.
"""

from __future__ import annotations

import decimal
import itertools

import pytest

from app.services import ledger
from app.services.billing import _meter_actual_cost
from app.services.unit_quotes import Learned, compute_unit_quote

D = decimal.Decimal
MANIFEST = {"billing_hints": {"unit": "frame", "est_gpu_seconds_per_unit": 30}}
PRICE, FEE = D("3.60"), D("0.10")


def _quote(units=100, gpus=1, max_cost=D("1000"), learned=None):
    return compute_unit_quote(manifest=MANIFEST, expected_units=units, gpu_count=gpus, price_per_hour=PRICE,
                              fee_rate=FEE, max_cost=max_cost, learned=learned)


def _held_minor(quote: dict) -> int:
    """The hold for a job linked to this quote: the quote's high estimate, never above its cap."""
    return min(ledger.to_minor(D(quote["gpu"]["high"])), ledger.to_minor(D(quote["max_cost"])))


def _captured(held_minor: int, seconds: D, gpus: int = 1) -> int:
    gpu, fee = _meter_actual_cost(price_per_hour=PRICE, gpu_count=gpus, fee_rate=FEE, actual_seconds=seconds)
    return min(gpu + fee, held_minor)  # settle_job: never capture beyond the held authorization


@pytest.mark.parametrize("max_cost,seconds", list(itertools.product([D("0.50"), D("2.00"), D("50.00"), D("1000")],
                                                                    [D("1"), D("600"), D("3000"), D("100000")])))
def test_a_job_never_exceeds_its_cap_whatever_it_runs(max_cost, seconds):
    q = _quote(max_cost=max_cost)
    assert all(D(v) <= max_cost for v in q["gpu"].values())
    assert _captured(_held_minor(q), seconds) <= ledger.to_minor(max_cost)


def test_runaway_job_is_clamped_at_the_hold_not_the_estimate():
    q = _quote(units=10, max_cost=D("5.00"))
    held = _held_minor(q)
    gpu, fee = _meter_actual_cost(price_per_hour=PRICE, gpu_count=1, fee_rate=FEE, actual_seconds=D(10_000_000))
    assert gpu + fee > held  # it ran far longer than quoted
    assert _captured(held, D(10_000_000)) == held <= ledger.to_minor(D("5.00"))


def test_underrun_is_billed_for_what_it_used_not_the_quote():
    q = _quote(units=100, max_cost=D("1000"))
    held = _held_minor(q)
    used = _captured(held, D(60))  # one GPU-minute
    gpu, fee = _meter_actual_cost(price_per_hour=PRICE, gpu_count=1, fee_rate=FEE, actual_seconds=D(60))
    assert used == gpu + fee < held  # metered GPU-seconds, far below the quoted amount


def test_billing_is_linear_in_gpu_seconds_and_independent_of_units():
    one, two = (_meter_actual_cost(price_per_hour=PRICE, gpu_count=1, fee_rate=FEE, actual_seconds=D(s)) for s in (3600, 7200))
    assert sum(two) == 2 * sum(one)
    # Quoting 10 or 10000 units changes the estimate and the hold, never the metered rate.
    for units in (10, 10_000):
        _quote(units=units)
        assert _meter_actual_cost(price_per_hour=PRICE, gpu_count=1, fee_rate=FEE, actual_seconds=D(3600)) == one
    four_gpu = _meter_actual_cost(price_per_hour=PRICE, gpu_count=4, fee_rate=FEE, actual_seconds=D(3600))
    assert four_gpu[0] == 4 * one[0]


def test_learned_correction_raises_the_estimate_but_not_the_cap():
    learned = Learned(D(3), 10, D(90))  # observed jobs ran 3x the hint
    q = _quote(units=100, max_cost=D("4.00"), learned=learned)
    assert q["capped"] is True and all(D(v) <= D("4.00") for v in q["gpu"].values())
    assert _held_minor(q) <= ledger.to_minor(D("4.00"))


def test_extras_are_not_part_of_the_hold():
    q = compute_unit_quote(manifest=MANIFEST, expected_units=100, gpu_count=1, price_per_hour=PRICE, fee_rate=FEE,
                           max_cost=D("1000"), egress_gb=D(500), storage_gb=D(500))
    assert q["gpu"] == _quote()["gpu"]
