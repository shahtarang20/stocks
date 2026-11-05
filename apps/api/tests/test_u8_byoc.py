"""U8 BYOC (written, not run): spec validation, scanner policy, placement gate."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import byoc as svc
from app.services import image_scan, scheduler_v2

IMG = "ghcr.io/acme/tool@sha256:" + "a" * 64


def _spec(**kw):
    s = {"version": 2, "mode": "byoc", "image": IMG, "command": ["python", "x.py"]}
    s.update(kw)
    return s


@pytest.fixture(autouse=True)
def _allow(monkeypatch):
    monkeypatch.setattr(settings, "BYOC_REGISTRY_ALLOWLIST", "ghcr.io/acme", raising=False)


def test_valid_spec():
    assert svc.validate_byoc_spec(_spec()).digest == "sha256:" + "a" * 64


@pytest.mark.parametrize("bad", [
    {"image": "ghcr.io/acme/tool:latest"}, {"image": "evil.io/x/y@sha256:" + "a" * 64},
    {"privileged": True}, {"mounts": ["/:/host"]}, {"env": {"COMPUTECOIN_X": "1"}}, {"env": {"SECRET_KEY": "1"}},
    {"command": ["x"] * 40}, {"network": {"egress": ["a.com:443"]}}, {"isolation": "container"},
])
def test_refused(bad):
    with pytest.raises(svc.ByocError):
        svc.validate_byoc_spec(_spec(**bad))


def test_empty_allowlist_refuses(monkeypatch):
    monkeypatch.setattr(settings, "BYOC_REGISTRY_ALLOWLIST", "", raising=False)
    with pytest.raises(svc.ByocError):
        svc.validate_byoc_spec(_spec())


def test_unconfigured_scanner_never_clean():
    r = asyncio.run(image_scan.UnconfiguredScanner().scan(IMG))
    assert r.status == image_scan.UNCONFIGURED and not r.clean


def test_trivy_policy():
    async def runner(argv, t):
        assert argv[0] == "trivy" and argv[-1] == IMG
        return 0, json.dumps({"Results": [{"Target": "x", "Packages": [{"Name": "xmrig"}]}]}).encode(), b""

    r = asyncio.run(image_scan.TrivyScanner(binary="trivy", runner=runner).scan(IMG))
    assert r.status == image_scan.DENIED

    async def bad(argv, t):
        return 1, b"", b"boom"

    assert asyncio.run(image_scan.TrivyScanner(binary="trivy", runner=bad).scan(IMG)).status == image_scan.ERROR
    assert asyncio.run(image_scan.TrivyScanner(binary="trivy", runner=bad).scan("-oops")).status == image_scan.ERROR


def test_scheduler_requires_advertised_runtime():
    job = SimpleNamespace(spec_v2={"byoc": {"isolation": "gvisor"}, "effective_resources": {}}, gpu_count=0)
    none = SimpleNamespace(last_health={}, hardware=None)
    ok = SimpleNamespace(last_health={"isolation_runtimes": ["gvisor"]}, hardware=None)
    assert scheduler_v2.machine_fit_reason(job, none) == "isolation_runtime"
    assert scheduler_v2.machine_fit_reason(job, ok) is None
