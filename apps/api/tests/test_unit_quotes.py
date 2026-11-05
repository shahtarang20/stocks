"""U15 unit quote math (pure; NOT YET RUN). Ledger-level proof of U15.1 still needs an integration test."""
import decimal

from app.services.unit_quotes import Learned, compute_unit_quote, learned_from_samples

D = decimal.Decimal
M = {"billing_hints": {"unit": "frame", "est_gpu_seconds_per_unit": 30}}


def _q(**kw):
    args = dict(manifest=M, expected_units=100, gpu_count=1, price_per_hour=D("3.6"), fee_rate=D("0"), max_cost=D("1000"))
    args.update(kw)
    return compute_unit_quote(**args)


def test_basic_range_and_confidence():
    q = _q()
    assert q["gpu"]["expected"] == "3.00"  # 3000 s * 3.6/3600
    assert q["confidence"] == "low" and not q["capped"]
    assert D(q["gpu"]["low"]) < D(q["gpu"]["expected"]) < D(q["gpu"]["high"])


def test_never_exceeds_cap():
    q = _q(max_cost=D("2.00"))
    assert q["capped"] and all(D(v) <= D("2.00") for v in q["gpu"].values())


def test_extras_separate_and_zero_by_default():
    q = _q(egress_gb=D(10), storage_gb=D(5))
    assert all(not li["charged"] and li["amount"] == "0.00" for li in q["line_items"])


def test_learned_never_below_observed():
    samples = [(D(300), D(600), 10)] * 5  # actual is 2x estimate, 60 s/unit
    L = learned_from_samples(samples)
    assert L.ratio == D(2) and L.floor_per_unit == D(60)
    assert _q(learned=L)["estimated_gpu_seconds"] == "6000.00"


def test_no_learning_below_min_samples():
    assert learned_from_samples([(D(1), D(2), 1)]).ratio == D(1)
