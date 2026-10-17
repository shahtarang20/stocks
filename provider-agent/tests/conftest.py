import sys
import types

import pytest


@pytest.fixture
def tmp_config_path(tmp_path):
    return tmp_path / "computecoin" / "agent.toml"


class FakeHandle:
    pass


def _install_fake_pynvml(monkeypatch, gpus):
    """Install a fake ``pynvml`` module exposing ``gpus`` GPUs."""
    mod = types.ModuleType("pynvml")

    mem_cls = types.SimpleNamespace

    mod.nvmlInit = lambda: None
    mod.nvmlShutdown = lambda: None
    mod.nvmlDeviceGetCount = lambda: len(gpus)

    def get_handle(i):
        h = FakeHandle()
        h._i = i
        return h

    mod.nvmlDeviceGetHandleByIndex = get_handle
    mod.nvmlDeviceGetName = lambda h: gpus[h._i]["name"]
    mod.nvmlDeviceGetMemoryInfo = lambda h: mem_cls(total=gpus[h._i]["mem_mb"] * 1024 * 1024)
    mod.nvmlDeviceGetUUID = lambda h: gpus[h._i].get("uuid", "GPU-fake")

    monkeypatch.setitem(sys.modules, "pynvml", mod)
    return mod


@pytest.fixture
def fake_nvml(monkeypatch):
    def _factory(gpus):
        return _install_fake_pynvml(monkeypatch, gpus)

    return _factory


@pytest.fixture
def no_nvml(monkeypatch):
    """Simulate a host where pynvml is not importable."""
    monkeypatch.setitem(sys.modules, "pynvml", None)
