"""The agent's real wire format (its own sample type and signing) is accepted by the server and shows up on the host's page.

If either side changes its format, the host's live page would silently go empty. This builds the request exactly the way the
agent's ``TelemetryCollector.flush`` does and sends it to the real endpoint.
"""

from __future__ import annotations

import json
import time

import pytest

from computecoin_agent.telemetry import TelemetrySample, sign_contract
from tests.helpers import auth_headers
from tests.test_phase3_marketplace import create_listable_machine


def _agent_request(machine_id: str, secret: str, samples):
    payload = {"points": [s.to_dict() for s in samples]}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": str(ts),
        "X-ComputeCoin-Signature": sign_contract(machine_id, ts, raw, secret),
    }
    return raw, headers


@pytest.mark.asyncio
async def test_a_batch_in_the_agents_own_format_lands_on_the_hosts_live_page(client):
    machine = await create_listable_machine(client, "p-wire@x.com", hostname="wire-test")
    now = time.time()
    samples = [
        TelemetrySample(gpu_index=0, gpu_util_pct=71.5, mem_util_pct=36.0, mem_used_mb=3000, mem_total_mb=8192, temp_c=66.0,
                        power_w=150.0, fan_speed_pct=40.0, timestamp=now - 5),
        TelemetrySample(gpu_index=1, gpu_util_pct=12.0, mem_util_pct=8.0, mem_used_mb=700, mem_total_mb=8192, temp_c=48.0,
                        power_w=60.0, fan_speed_pct=30.0, timestamp=now - 5),
    ]
    raw, headers = _agent_request(machine["machine_id"], machine["secret"], samples)
    res = await client.post("/v1/telemetry/machine", content=raw, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["accepted"] == 2

    live = (await client.get(f"/provider/machines/{machine['machine_id']}/live", headers=auth_headers(machine["access"]))).json()
    assert live["readings_recent"] is True
    assert [(g["gpu_index"], g["gpu_util_pct"], g["mem_used_mb"], g["temp_c"]) for g in live["gpus"]] == [
        (0, 71.5, 3000, 66.0), (1, 12.0, 700, 48.0),
    ]

    usage = (await client.get(f"/provider/machines/{machine['machine_id']}/usage", headers=auth_headers(machine["access"]))).json()
    assert usage["telemetry_available"] is True  # the agent's readings turned "not reporting" into a real measurement


@pytest.mark.asyncio
async def test_a_batch_signed_with_the_wrong_secret_is_refused(client):
    machine = await create_listable_machine(client, "p-wire2@x.com", hostname="wire-test-2")
    raw, headers = _agent_request(machine["machine_id"], "not-the-machine-secret", [TelemetrySample(gpu_util_pct=5.0, timestamp=time.time())])
    res = await client.post("/v1/telemetry/machine", content=raw, headers=headers)
    assert res.status_code in (401, 403)
