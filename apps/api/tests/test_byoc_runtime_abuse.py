"""U8/U16: BYOC runtime miner signals and the ClamAV scanner. WRITTEN, NOT YET RUN. Pure functions and fakes only."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import os
import tarfile

import pytest

from app.core.config import settings
from app.services import abuse, image_scan

REF = "registry.example.com/org/img@sha256:" + "a" * 64
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _s(i, *, util=100.0, io_bytes=0, power=250.0, progress=None, step=None, gap=60):
    return {"ts": T0 + dt.timedelta(seconds=i * gap), "util": util, "mem": 1000, "step": step, "progress": progress,
            "io_bytes": io_bytes, "power_w": power}


def test_stratum_and_pool_port_detected():
    assert abuse.match_stratum(["stratum+tcp://pool.example.org:3333", "example.com:443"]) == [
        "stratum+tcp://pool.example.org:3333"]
    assert abuse.match_stratum(["host.example:14444"]) and not abuse.match_stratum(["pypi.org:443", 5])


def test_miner_binary_names_matched_from_process_list():
    assert abuse.match_processes(["python3", "/tmp/xmrig", "T-Rex.exe"]) == ["/tmp/xmrig", "T-Rex.exe"]


def test_full_util_zero_io_no_progress_flags(monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_BYOC_IDLE_IO_WINDOW_SECONDS", 600)
    rows = [_s(i) for i in range(11)]  # 600 s, constant io
    assert abuse.SIG_BYOC_IDLE_IO in [s for s, _ in abuse.evaluate_byoc_samples(rows)]


def test_io_growth_or_progress_or_missing_io_does_not_flag(monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_BYOC_IDLE_IO_WINDOW_SECONDS", 600)
    sigs = lambda rows: [s for s, _ in abuse.evaluate_byoc_samples(rows)]  # noqa: E731
    assert abuse.SIG_BYOC_IDLE_IO not in sigs([_s(i, io_bytes=i * 10_000_000) for i in range(11)])
    assert abuse.SIG_BYOC_IDLE_IO not in sigs([_s(i, progress=float(i)) for i in range(11)])
    assert abuse.SIG_BYOC_IDLE_IO not in sigs([_s(i, io_bytes=None) for i in range(11)])  # no telemetry, no verdict
    assert abuse.SIG_BYOC_IDLE_IO not in sigs([_s(i, util=60.0) for i in range(11)])


def test_identical_high_power_flags_but_varying_power_does_not(monkeypatch):
    monkeypatch.setattr(settings, "ABUSE_BYOC_POWER_WINDOW_SECONDS", 600)
    flat = [_s(i, power=251.0 + (0.2 if i % 2 else 0)) for i in range(11)]
    assert abuse.SIG_BYOC_POWER in [s for s, _ in abuse.evaluate_byoc_samples(flat)]
    wavy = [_s(i, power=200.0 + 30 * (i % 3)) for i in range(11)]
    assert abuse.SIG_BYOC_POWER not in [s for s, _ in abuse.evaluate_byoc_samples(wavy)]
    idle = [_s(i, power=20.0) for i in range(11)]
    assert abuse.SIG_BYOC_POWER not in [s for s, _ in abuse.evaluate_byoc_samples(idle)]


# ---- ClamAV scanner ------------------------------------------------------- #
def _docker_save_tar(path: str, files: dict[str, bytes], *, evil_layer_member: str | None = None) -> None:
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as lt:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            lt.addfile(ti, io.BytesIO(data))
        if evil_layer_member:
            ti = tarfile.TarInfo(evil_layer_member)
            ti.size = 1
            lt.addfile(ti, io.BytesIO(b"x"))
    with tarfile.open(path, "w") as t:
        for name, data in (("layer0/layer.tar", layer.getvalue()),
                           ("manifest.json", json.dumps([{"Layers": ["layer0/layer.tar"]}]).encode())):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))


class _Runner:
    def __init__(self, files, *, scan_code=0, scan_out=b"", save_code=0, **kw):
        self.files, self.scan_code, self.scan_out, self.save_code, self.kw = files, scan_code, scan_out, save_code, kw
        self.calls: list[list[str]] = []
        self.workdirs: list[str] = []
        self.scanned: list[str] = []

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        assert all(isinstance(a, str) for a in argv)
        if argv[1] == "save":
            if self.save_code == 0:
                _docker_save_tar(argv[3], self.files, **self.kw)
                self.workdirs.append(os.path.dirname(argv[3]))
            return self.save_code, b"", b""
        if argv[1] == "pull":
            return 1, b"", b""
        root = argv[-1]
        for dp, _dn, fn in os.walk(root):
            self.scanned += [os.path.join(dp, f) for f in fn]
        return self.scan_code, self.scan_out, b""


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_clamav_clean_scans_exported_tree_and_wipes_tmp():
    r = _Runner({"app/run.py": b"print(1)"})
    res = _run(image_scan.ClamAVScanner(runner=r).scan(REF))
    assert res.clean and res.scanner == "clamav"
    assert any(p.endswith("app/run.py") for p in r.scanned)
    assert r.calls[0][0] == "docker" and r.calls[-1][0] == "clamscan"  # argv lists, no shell
    assert not os.path.exists(r.workdirs[0])


def test_clamav_infected_is_denied_with_signature():
    out = b"/tmp/cc-clamav-x/rootfs/usr/bin/xm: Win.Miner.Gen-1 FOUND\n"
    res = _run(image_scan.ClamAVScanner(runner=_Runner({"a": b"b"}, scan_code=1, scan_out=out)).scan(REF))
    assert res.status == image_scan.DENIED and res.findings[0]["signature"] == "Win.Miner.Gen-1"


def test_clamav_failures_are_errors_never_clean():
    assert _run(image_scan.ClamAVScanner(runner=_Runner({}, scan_code=2)).scan(REF)).status == image_scan.ERROR
    assert _run(image_scan.ClamAVScanner(runner=_Runner({}, save_code=1)).scan(REF)).status == image_scan.ERROR
    assert _run(image_scan.ClamAVScanner(runner=_Runner({"a": b"b"}), max_bytes=1).scan(REF)).status == image_scan.ERROR
    assert _run(image_scan.ClamAVScanner(runner=_Runner({})).scan("--evil")).status == image_scan.ERROR


def test_clamav_refuses_path_escape_in_a_layer():
    r = _Runner({"a": b"b"}, evil_layer_member="../../escape")
    res = _run(image_scan.ClamAVScanner(runner=r).scan(REF))
    assert res.status == image_scan.ERROR and res.findings[0]["code"] == "image_unpack_failed"
    assert not os.path.exists(r.workdirs[0])


class _Fixed(image_scan.ImageScanner):
    def __init__(self, name, status):
        self.name, self._st = name, status

    async def scan(self, image_ref):
        return image_scan.ScanResult(status=self._st, scanner=self.name)


def test_combined_requires_all_clean():
    comb = lambda *st: image_scan.CombinedScanner([_Fixed(f"s{i}", x) for i, x in enumerate(st)])  # noqa: E731
    assert _run(comb("clean", "clean").scan(REF)).clean
    assert _run(comb("clean", "denied").scan(REF)).status == "denied"
    assert _run(comb("error", "denied").scan(REF)).status == "denied"
    assert _run(comb("clean", "error").scan(REF)).status == "error"
    assert _run(comb("clean", "unconfigured").scan(REF)).status == "unconfigured"


@pytest.mark.parametrize("choice,cls", [("clamav", "ClamAVScanner"), ("trivy+clamav", "CombinedScanner"),
                                        ("", "UnconfiguredScanner"), ("trivy", "TrivyScanner")])
def test_scanner_selection(monkeypatch, choice, cls):
    image_scan.set_scanner(None)
    monkeypatch.setattr(settings, "BYOC_SCANNER", choice)
    assert type(image_scan.get_scanner()).__name__ == cls
