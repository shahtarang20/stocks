"""BYOC escape tests (U8.3). Opt in with COMPUTECOIN_ESCAPE_TESTS=1. Written, never run in the change that added them.

Needs: docker, a probe image pinned by digest in COMPUTECOIN_ESCAPE_IMAGE (any image with ``python3`` on PATH, for example
``python@sha256:...``; it is pulled by docker as usual), and runsc (gVisor) and/or Kata for the tiers under test.
The container argv comes from the REAL ``DockerExecutor.build_argv``, so the tests exercise the production flags.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from computecoin_agent.executor import DockerExecutor, JobSpec
from computecoin_agent.isolation import probe_gvisor, probe_microvm

ENABLED = os.environ.get("COMPUTECOIN_ESCAPE_TESTS") == "1"
IMAGE = os.environ.get("COMPUTECOIN_ESCAPE_IMAGE", "")

pytestmark = pytest.mark.skipif(not ENABLED, reason="set COMPUTECOIN_ESCAPE_TESTS=1 to run container escape tests")


def pytest_collection_modifyitems(config, items):  # skip everything in this directory unless opted in
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="set COMPUTECOIN_ESCAPE_TESTS=1 to run container escape tests")
    for item in items:
        if "escape" in str(item.fspath):
            item.add_marker(skip)


def _tiers():
    out = []
    if not ENABLED or probe_gvisor():
        out.append("gvisor")
    if ENABLED and probe_microvm():
        out.append("microvm")
    return out


@pytest.fixture(params=_tiers() or ["gvisor"])
def isolation(request):
    return request.param


@pytest.fixture
def run_probe(isolation):
    """Run a python probe inside a BYOC container; returns (returncode, stdout, stderr)."""
    if not shutil.which("docker"):
        pytest.skip("docker not installed")
    if not IMAGE or "@sha256:" not in IMAGE:
        pytest.skip("set COMPUTECOIN_ESCAPE_IMAGE to a digest-pinned image that has python3")

    def _run(code: str, *, timeout: int = 120):
        outdir = tempfile.mkdtemp(prefix="ccoin-escape-")
        os.chmod(outdir, 0o777)
        try:
            spec = JobSpec(
                job_id="escape-test", workload_type="byoc", model="byoc", input={}, execution_mode="hardware",
                image_digest=IMAGE, isolation_tier="container",
                v2={"spec_version": 2, "kit": {"slug": "byoc", "version": "1"}, "byoc": {
                    "image": IMAGE, "isolation": isolation, "runtime": "runsc", "command": ["python3", "-c", code],
                    "args": [], "env": {}, "gpu_count": 0, "memory_mb": 256, "cpus": "1", "pids_limit": 64,
                    "max_runtime_seconds": timeout}},
            )
            ex = DockerExecutor()
            argv = ex.build_argv(spec, outdir)
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 30,
                                  env={**os.environ, "COMPUTECOIN_MACHINE_SECRET": "host-canary-secret"})
            return proc.returncode, proc.stdout, proc.stderr
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    return _run


def assert_blocked(result):
    code, out, err = result
    assert "ESCAPED" not in out, f"container escape succeeded: {out!r}"
    assert "BLOCKED" in out, f"probe did not run to completion (rc={code}): {out!r} {err[-500:]!r}"
