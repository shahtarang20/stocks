"""U16/U19 unit tests (written, not executed in the authoring session)."""

from datetime import timedelta

from app.db.base import utcnow
from app.services import abuse


def _samples(n, span, util=100.0, step=None, progress=None):
    t0 = utcnow()
    return [
        {"ts": t0 + timedelta(seconds=span * i / (n - 1)), "util": util, "mem": 1000,
         "step": step, "progress": progress}
        for i in range(n)
    ]


def test_sustained_util_no_progress_flags():
    sigs = [s for s, _ in abuse.evaluate_samples(_samples(10, 1000))]
    assert abuse.SIG_UTIL_NO_PROGRESS in sigs


def test_progress_advancing_not_flagged():
    smp = _samples(10, 1000)
    for i, s in enumerate(smp):
        s["progress"] = i * 10.0
        s["mem"] = 1000 + i
    assert abuse.evaluate_samples(smp) == []


def test_short_window_not_flagged():
    assert abuse.evaluate_samples(_samples(10, 60)) == []


def test_egress_and_process_matching():
    assert abuse.match_egress(["pool.nanopool.org:443", "example.com:3333", "pypi.org:443"]) == [
        "pool.nanopool.org:443", "example.com:3333"]
    assert abuse.match_processes(["/usr/bin/xmrig", "python"]) == ["/usr/bin/xmrig"]
