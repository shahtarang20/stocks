"""Scheduler quality: reliability now comes from real completion history and
influences ranking (previously a constant 0.5)."""
import datetime as dt
import decimal
import uuid
from types import SimpleNamespace

from app.services.scheduler import _Candidate, _score_candidates


def _cand(price: str, max_slots: int = 4, free: int = 4):
    machine = SimpleNamespace(
        id=uuid.uuid4(), max_concurrent_jobs=max_slots,
        created_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
    )
    return _Candidate(machine=machine, used_slots=max_slots - free, free_slots=free,
                      price=decimal.Decimal(price))


def test_reliability_breaks_ties_between_equal_price_machines():
    a = _cand("1.50")
    b = _cand("1.50")
    # Same price and availability; A has a much better completion history.
    scored = _score_candidates([a, b], {a.machine.id: 0.95, b.machine.id: 0.10})
    assert scored[0][0].machine.id == a.machine.id
    assert scored[0][2]["reliability_score"] == 0.95
    assert scored[-1][0].machine.id == b.machine.id


def test_unknown_machine_defaults_to_neutral_reliability():
    a = _cand("1.50")
    scored = _score_candidates([a], {})  # no history -> 0.5 prior
    assert scored[0][2]["reliability_score"] == 0.5


def test_reliability_shifts_score_at_equal_price():
    # At equal price+availability, a more reliable machine gets a strictly higher
    # total score (reliability contributes w_rel * reliability_score).
    a = _cand("1.50")
    b = _cand("1.50")
    scored = _score_candidates([a, b], {a.machine.id: 0.9, b.machine.id: 0.3})
    by_id = {c.machine.id: score for c, score, _ in scored}
    assert by_id[a.machine.id] > by_id[b.machine.id]
