"""Shared pytest setup for tests/windows (cross-platform plan W15). WRITTEN, NOT RUN.

Two kinds of test live here:

* **Hosted-runner safe** (no marker): operator tooling, the parity hash comparison, helper logic. They run in
  ``.github/workflows/ci.yml`` on ``windows-latest`` and on Linux.
* **``windows_gpu``**: need a real Windows host with an NVIDIA GPU and the managed WSL2 distro. They run only from
  ``.github/workflows/windows-host-e2e.yml`` on the self-hosted runner labelled ``windows-gpu``, which sets
  ``COMPUTECOIN_WINDOWS_GPU_E2E=1``. Anywhere else they are skipped with a reason, never silently passed.

Every command, image and path these tests use comes from the environment (see ``e2e_support.Settings``); no secret is ever a
default value, a parameter or part of a failure message (L7).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def pytest_configure(config):
    config.addinivalue_line("markers", "windows_gpu: needs a real Windows host with an NVIDIA GPU and the managed WSL2 distro (self-hosted runner)")
    config.addinivalue_line("markers", "windows_only: only meaningful on Windows")
    config.addinivalue_line("markers", "soak: long-running soak test, opt in with COMPUTECOIN_SOAK_HOURS")


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    gpu_enabled = os.environ.get("COMPUTECOIN_WINDOWS_GPU_E2E") == "1"
    for item in items:
        if "windows_gpu" in item.keywords and not gpu_enabled:
            item.add_marker(pytest.mark.skip(reason="needs the self-hosted windows-gpu runner (COMPUTECOIN_WINDOWS_GPU_E2E=1)"))
        if "windows_only" in item.keywords and sys.platform != "win32":
            item.add_marker(pytest.mark.skip(reason="Windows only"))
        if "soak" in item.keywords and not os.environ.get("COMPUTECOIN_SOAK_HOURS"):
            item.add_marker(pytest.mark.skip(reason="set COMPUTECOIN_SOAK_HOURS to run the soak"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def settings():
    from e2e_support import Settings

    return Settings.from_env()
