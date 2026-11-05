"""infrastructure/observability: every metric an alert rule reads must be scraped by some job."""

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[3]
OBS = ROOT / "infrastructure" / "observability"
APP = ROOT / "apps" / "api" / "app"


def _exposed(path: pathlib.Path) -> set[str]:
    return set(re.findall(r"computecoin_[a-z_]+", path.read_text()))


def test_every_alerted_metric_is_exposed_by_a_scraped_endpoint():
    prom = yaml.safe_load((OBS / "prometheus.yml").read_text())
    scraped_paths = {j["metrics_path"] for j in prom["scrape_configs"]}
    sources = {"/v1/ops/metrics": _exposed(APP / "services" / "observability.py"), "/metrics": _exposed(APP / "api" / "routers" / "metrics.py")}
    available = set().union(*(m for p, m in sources.items() if p in scraped_paths))
    alerts = yaml.safe_load((OBS / "alerts.yml").read_text())
    for group in alerts["groups"]:
        for rule in group["rules"]:
            for metric in re.findall(r"computecoin_[a-z_]+", rule["expr"]):
                assert metric in available, f"{rule['alert']} reads {metric}, which no scrape job collects"
