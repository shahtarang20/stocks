"""/metrics endpoint: Prometheus-format operational gauges incl. ledger imbalance."""
import pytest


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_gauges(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    # Core gauges present.
    assert "computecoin_jobs_total" in body
    assert "computecoin_machines_online" in body
    assert "computecoin_reservations_active" in body
    assert "computecoin_users_total" in body
    # The money-safety metric must be present and zero on a balanced ledger.
    assert "computecoin_ledger_imbalance_minor 0" in body


@pytest.mark.asyncio
async def test_metrics_token_enforced_when_configured(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "METRICS_TOKEN", "s3cret-metrics-token")
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer s3cret-metrics-token"})).status_code == 200
