"""W10 (cross-platform plan): the `platform` label on fleet metrics and the Windows alert rules. WRITTEN, NOT RUN.

Pure functions plus the alert file: no database. Bug re-insertion: copy the platform string from the heartbeat instead of
the allowlist (the injection test fails); count signals of a stale host (the stale test fails); rename a series in
observability.py without the alert (the existing test_observability_config and the rule test below fail).
"""

from __future__ import annotations

import pathlib
import re
from datetime import timedelta

import pytest

from app.core.config import settings
from app.db.base import utcnow
from app.services import observability as obs

yaml = pytest.importorskip("yaml")
ROOT = pathlib.Path(__file__).resolve().parents[3]
OBS = ROOT / "infrastructure" / "observability"


def test_platform_label_comes_from_an_allowlist_never_from_the_host_text():
    assert obs.machine_platform({"platform": {"os": "windows"}}) == "windows"
    assert obs.machine_platform({"platform": {"os": "linux"}}) == "linux"
    for odd in ({"platform": {"os": 'windows"} 1\nfake_metric{x="'}}, {"platform": {"os": "WINDOWS"}}, {"platform": "windows"},
                {"platform": {"os": 7}}, {"platform": {}}, {}, None, [], "windows"):
        assert obs.machine_platform(odd) == "unknown"


def test_signals_need_an_explicit_boolean():
    assert obs.platform_signals({}) == {
        "distro_unhealthy": False, "gpu_smoke_failing": False, "update_rolled_back": False, "yielding": False}
    assert obs.platform_signals({"runtime": {"distro_ok": False, "gpu_smoke_ok": False}, "update": {"rolled_back": True},
                                 "availability_detail": {"state": "yielding"}}) == {
        "distro_unhealthy": True, "gpu_smoke_failing": True, "update_rolled_back": True, "yielding": True}
    # Truthy-but-not-boolean values are ignored: only True / False count.
    assert obs.platform_signals({"runtime": {"distro_ok": 0, "gpu_smoke_ok": "no"}, "update": {"rolled_back": 1}}) == {
        "distro_unhealthy": False, "gpu_smoke_failing": False, "update_rolled_back": False, "yielding": False}
    assert obs.platform_signals({"runtime": {"distro_ok": True, "gpu_smoke_ok": True}})["distro_unhealthy"] is False


def _lines(rows):
    return obs.render_platform_metrics(rows, utcnow())


def _value(lines, series):
    hit = [ln for ln in lines if ln.startswith(series + " ")]
    assert len(hit) == 1, (series, hit)
    return int(hit[0].rsplit(" ", 1)[1])


def test_machines_by_platform_counts_each_platform_separately():
    now = utcnow()
    rows = [
        ("online", {"platform": {"os": "windows"}}, now),
        ("online", {"platform": {"os": "windows"}}, now),
        ("throttled", {"platform": {"os": "windows"}}, now),
        ("online", {"platform": {"os": "linux"}}, now),
        ("online", {}, now),  # an agent from before W1
    ]
    lines = _lines(rows)
    assert _value(lines, 'computecoin_ops_machines_by_platform{availability="online",platform="windows"}') == 2
    assert _value(lines, 'computecoin_ops_machines_by_platform{availability="throttled",platform="windows"}') == 1
    assert _value(lines, 'computecoin_ops_machines_by_platform{availability="online",platform="linux"}') == 1
    assert _value(lines, 'computecoin_ops_machines_by_platform{availability="online",platform="unknown"}') == 1
    assert _value(lines, 'computecoin_ops_machines_by_platform{availability="offline",platform="windows"}') == 0


def test_windows_gauges_count_only_hosts_that_reported_a_failure():
    now = utcnow()
    win = {"platform": {"os": "windows"}}
    rows = [
        ("online", {**win, "runtime": {"distro_ok": False, "gpu_smoke_ok": True}}, now),
        ("online", {**win, "runtime": {"distro_ok": True, "gpu_smoke_ok": False}}, now),
        ("online", {**win, "update": {"rolled_back": True}}, now),
        ("throttled", {**win, "availability_detail": {"state": "yielding"}}, now),
        ("online", win, now),
    ]
    lines = _lines(rows)
    assert _value(lines, 'computecoin_ops_platform_distro_unhealthy{platform="windows"}') == 1
    assert _value(lines, 'computecoin_ops_platform_gpu_smoke_failing{platform="windows"}') == 1
    assert _value(lines, 'computecoin_ops_platform_update_rollbacks{platform="windows"}') == 1
    assert _value(lines, 'computecoin_ops_platform_hosts_yielding{platform="windows"}') == 1
    assert _value(lines, 'computecoin_ops_platform_distro_unhealthy{platform="linux"}') == 0


def test_a_stale_host_does_not_keep_an_alert_firing():
    old = utcnow() - timedelta(seconds=settings.ONLINE_WINDOW_SECONDS * 3 + 60)
    rows = [("offline", {"platform": {"os": "windows"}, "runtime": {"distro_ok": False}}, old),
            ("online", {"platform": {"os": "windows"}, "runtime": {"distro_ok": False}}, None)]
    lines = _lines(rows)
    assert _value(lines, 'computecoin_ops_platform_distro_unhealthy{platform="windows"}') == 0
    # They still count as machines of that platform.
    assert _value(lines, 'computecoin_ops_machines_by_platform{availability="offline",platform="windows"}') == 1


def test_exposition_has_one_type_line_per_metric_and_no_foreign_text():
    lines = _lines([("online", {"platform": {"os": 'x"\n# TYPE evil gauge'}}, utcnow())])
    types = [ln for ln in lines if ln.startswith("# TYPE")]
    assert len(types) == len({t.split()[2] for t in types})
    assert not any("evil" in ln for ln in lines)
    for ln in lines:
        if not ln.startswith("#"):
            assert re.fullmatch(r'computecoin_[a-z_]+\{[^}]*\} \d+', ln), ln


def test_windows_alert_group_exists_and_reads_exposed_series():
    alerts = yaml.safe_load((OBS / "alerts.yml").read_text(encoding="utf-8"))
    group = next(g for g in alerts["groups"] if g["name"] == "computecoin-windows")
    names = {r["alert"] for r in group["rules"]}
    assert {"WindowsDistroUnhealthy", "WindowsGpuSmokeTestFailing", "WindowsUpdateRollbacks", "WindowsHostsMostlyYielding"} <= names
    source = pathlib.Path(obs.__file__).read_text(encoding="utf-8")
    for rule in group["rules"]:
        assert 'platform="windows"' in rule["expr"], rule["alert"]
        for metric in re.findall(r"computecoin_[a-z_]+", rule["expr"]):
            assert metric in source, f"{rule['alert']} reads {metric}, which observability.py does not expose"
        assert rule["labels"]["severity"] in {"info", "warning", "critical"}


def test_grafana_dashboard_has_the_windows_panels_with_unique_ids():
    import json

    dash = json.loads((OBS / "grafana" / "computecoin-platform.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    titles = " | ".join(p["title"] for p in dash["panels"])
    for needle in ("Machines by platform", "yielding", "distro unhealthy", "GPU smoke", "update rollbacks"):
        assert needle in titles
    source = pathlib.Path(obs.__file__).read_text(encoding="utf-8")
    for p in dash["panels"]:
        for t in p.get("targets", []):
            for metric in re.findall(r"computecoin_[a-z_]+", t["expr"]):
                assert metric in source, f"panel {p['title']!r} reads {metric}"
