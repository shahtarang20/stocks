"""U13 agent: device sets, shm size, NCCL env (written, not run)."""

import pytest

from computecoin_agent.gpu_alloc import GpuAllocationError, GpuAllocator, nccl_env, nvidia_gpus_args, shm_size_mb


def test_allocator_never_shares_cards():
    a = GpuAllocator(4)
    first = a.acquire(2)
    assert first == [0, 1] and a.acquire(2) == [2, 3]
    with pytest.raises(GpuAllocationError):
        a.acquire(1)
    a.release(first)
    assert a.acquire(1) == [0]


def test_device_arg_and_shm_bounds():
    assert nvidia_gpus_args([2, 0, 1, 3]) == ["--gpus", '"device=0,1,2,3"']
    with pytest.raises(GpuAllocationError):
        nvidia_gpus_args([])
    assert shm_size_mb(8192) == 2048
    assert shm_size_mb(1024, {"shm_mb": 999999}) == 512  # at most half the memory limit
    assert shm_size_mb(10**6, {"shm_mb": 10**6}) == 16384


def test_nccl_env_single_and_multinode():
    assert nccl_env(4)["NCCL_IB_DISABLE"] == "1"
    rv = {"master_addr": "10.0.0.1", "master_port": 29500, "node_rank": 1, "nnodes": 2, "gpus_per_node": 4, "world_size": 8}
    env = nccl_env(4, rv)
    assert env["MASTER_ADDR"] == "10.0.0.1" and env["WORLD_SIZE"] == "8"
    with pytest.raises(GpuAllocationError):
        nccl_env(4, {"master_addr": "x"})
