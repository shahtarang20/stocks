"""Inside WSL, Docker supports only `--gpus all` (NVIDIA's CUDA-on-WSL guide, read 2026-11-17), so the GPU argument must not name a count there."""

from __future__ import annotations

import pytest

from computecoin_agent.executor import build_gpu_argv
from computecoin_agent.platform.wsl_env import running_in_wsl

WSL_KERNEL = "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@1234) (gcc (GCC) 11.2.0) #1 SMP"
AZURE_KERNEL = "Linux version 6.5.0-1018-azure (buildd@lcy02) (x86_64-linux-gnu-gcc-12 (Ubuntu 12.3.0) 12.3.0) #18~22.04.1-Ubuntu SMP"
PLAIN_KERNEL = "Linux version 6.8.0-45-generic (buildd@lcy02-amd64) (gcc (Ubuntu 13.2.0) 13.2.0) #45-Ubuntu SMP"


def test_a_wsl_kernel_or_the_wsl_variable_means_wsl():
    assert running_in_wsl({}, WSL_KERNEL) is True
    assert running_in_wsl({"WSL_DISTRO_NAME": "computecoin"}, PLAIN_KERNEL) is True


@pytest.mark.parametrize("kernel", [PLAIN_KERNEL, AZURE_KERNEL, "", "Linux version 5.4.0-microsoft-azure"])
def test_an_ordinary_linux_kernel_is_not_wsl(kernel):
    assert running_in_wsl({}, kernel) is False


def test_an_empty_wsl_variable_does_not_count_and_an_unreadable_kernel_file_is_not_wsl(monkeypatch):
    assert running_in_wsl({"WSL_DISTRO_NAME": "  "}, PLAIN_KERNEL) is False
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("no /proc")))
    assert running_in_wsl({}) is False  # never raises


def test_nvidia_asks_for_a_count_everywhere_except_inside_wsl():
    assert build_gpu_argv("nvidia", 1, in_wsl=False) == ["--gpus", "1"]
    assert build_gpu_argv("nvidia", 2, in_wsl=False) == ["--gpus", "2"]  # unchanged for every Linux host
    assert build_gpu_argv("nvidia", 1, in_wsl=True) == ["--gpus", "all"]
    assert build_gpu_argv("nvidia", 2, in_wsl=True) == ["--gpus", "all"]


def test_the_default_is_to_look_at_this_system(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "computecoin")
    assert build_gpu_argv("nvidia", 1) == ["--gpus", "all"]
    monkeypatch.delenv("WSL_DISTRO_NAME")
    monkeypatch.setattr("computecoin_agent.platform.wsl_env.running_in_wsl", lambda *a, **k: False)
    assert build_gpu_argv("nvidia", 3) == ["--gpus", "3"]


def test_amd_is_untouched_by_the_wsl_rule():
    with pytest.raises(Exception):  # no render nodes known: still refused exactly as before
        build_gpu_argv("amd", 1, render_nodes=[], in_wsl=True)
