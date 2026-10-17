"""Tests for Phase 9: Telemetry collection and batch reporting."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from computecoin_agent.heartbeat import verify_contract_signature
from computecoin_agent.telemetry import (
    TelemetryCollector,
    TelemetrySample,
    sample_gpu_metrics,
)


def test_sample_gpu_metrics_fallback():
    samples = sample_gpu_metrics(nvml_module=None, simulated=True)
    assert len(samples) >= 1
    sample = samples[0]
    assert sample.gpu_index == 0
    assert sample.gpu_util_pct is not None
    assert 0.0 <= sample.gpu_util_pct <= 100.0
    assert sample.temp_c is not None
    assert sample.mem_used_mb is not None
    assert sample.mem_total_mb is not None

    d = sample.to_dict()
    assert "timestamp" in d
    assert "gpu_util_pct" in d


def test_sample_gpu_metrics_mock_nvml():
    mock_nvml = MagicMock()
    mock_nvml.nvmlDeviceGetCount.return_value = 2

    handle0 = MagicMock()
    handle1 = MagicMock()
    mock_nvml.nvmlDeviceGetHandleByIndex.side_effect = [handle0, handle1]

    # Utilization
    mock_nvml.nvmlDeviceGetUtilizationRates.side_effect = [
        MagicMock(gpu=85),
        MagicMock(gpu=42),
    ]
    # Memory: 80GB total
    mock_nvml.nvmlDeviceGetMemoryInfo.side_effect = [
        MagicMock(total=85899345920, used=42949672960),  # 80GB total, 40GB used (50%)
        MagicMock(total=85899345920, used=21474836480),  # 80GB total, 20GB used (25%)
    ]
    mock_nvml.nvmlDeviceGetTemperature.side_effect = [72, 65]
    mock_nvml.nvmlDeviceGetPowerUsage.side_effect = [320000, 210000]  # milliwatts -> 320W, 210W
    mock_nvml.nvmlDeviceGetFanSpeed.side_effect = [60, 45]

    samples = sample_gpu_metrics(nvml_module=mock_nvml, simulated=False)
    assert len(samples) == 2

    s0 = samples[0]
    assert s0.gpu_index == 0
    assert s0.gpu_util_pct == 85.0
    assert s0.mem_util_pct == 50.0
    assert s0.temp_c == 72.0
    assert s0.power_w == 320.0
    assert s0.fan_speed_pct == 60.0

    s1 = samples[1]
    assert s1.gpu_index == 1
    assert s1.gpu_util_pct == 42.0
    assert s1.mem_util_pct == 25.0
    assert s1.temp_c == 65.0
    assert s1.power_w == 210.0


def test_telemetry_collector_buffer_and_flush():
    machine_id = "test-mach-id"
    machine_secret = "msk_secret_key_123"

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        # Verify HMAC signature
        ts = int(request.headers["X-ComputeCoin-Timestamp"])
        sig = request.headers["X-ComputeCoin-Signature"]
        assert verify_contract_signature(machine_id, ts, request.content, sig, machine_secret)

        body = json.loads(request.content.decode("utf-8"))
        assert len(body["points"]) == 2
        return httpx.Response(200, json={"accepted": 2, "alerts_triggered": 0})

    transport = httpx.MockTransport(mock_handler)
    collector = TelemetryCollector(
        machine_id=machine_id,
        machine_secret=machine_secret,
        base_url="http://api.test",
    )

    collector.record_sample(TelemetrySample(gpu_index=0, gpu_util_pct=50.0, temp_c=55.0))
    collector.record_sample(TelemetrySample(gpu_index=0, gpu_util_pct=60.0, temp_c=58.0))
    assert collector.buffer_size == 2

    res = collector.flush(transport=transport)
    assert res["accepted"] == 2
    assert collector.buffer_size == 0
    assert len(captured_requests) == 1


def test_telemetry_collector_retains_buffer_on_error():
    collector = TelemetryCollector(
        machine_id="test-mach-id",
        machine_secret="msk_secret_key_123",
        base_url="http://api.test",
    )
    collector.record_sample(TelemetrySample(gpu_index=0, gpu_util_pct=90.0, temp_c=88.0))
    assert collector.buffer_size == 1

    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal error"})

    transport = httpx.MockTransport(err_handler)
    with pytest.raises(httpx.HTTPStatusError):
        collector.flush(transport=transport)

    # Buffer must have retained the item
    assert collector.buffer_size == 1
