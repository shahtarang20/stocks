"""Automatic saving of rental results, the miner command-line net, and downloads that continue where they stopped."""

from __future__ import annotations

import hashlib
import os
import sys
import time

import httpx
import pytest

from computecoin_agent import rental as r
from computecoin_agent import runtime_v2 as rv
from test_container_rental_agent import (  # noqa: F401  (_fast is an autouse fixture)
    SID, FakeApi, Runner, _beats, _claim, _fast, _plant, _run_for, agent_for, fake_upload, run_serve,
)


linux_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="container rental is Linux-only (a Windows host never reports the flag)")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- unchanged files are not sent again ---------------------------------------------------------------------------------
def _put(run, name, data, *, mtime=None):
    path = os.path.join(run.workspace.path, "results", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(data)
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime))


@linux_only
def test_only_new_or_changed_files_are_sent_again(tmp_path):
    run = _run_for(tmp_path)
    store = []
    api = FakeApi()
    _put(run, "a.bin", b"aaa", mtime=1_000_000_000)
    _put(run, "sub/b.bin", b"bbbb", mtime=1_000_000_000)
    first = r.export_results(api, SID, run, upload=fake_upload(store))
    assert sorted(f["name"] for f in first.files) == ["a.bin", "sub/b.bin"] and not first.failed
    again = r.export_results(api, SID, run, upload=fake_upload(store))
    assert again.files == [] and len(store) == 2  # nothing changed: nothing sent
    _put(run, "a.bin", b"aaaaa", mtime=2_000_000_000)  # changed
    _put(run, "new.bin", b"n")  # new
    third = r.export_results(api, SID, run, upload=fake_upload(store))
    assert sorted(f["name"] for f in third.files) == ["a.bin", "new.bin"] and len(store) == 4
    _put(run, "sub/b.bin", b"BBBB", mtime=1_000_000_000)  # same size, same time, different bytes: a limit we state, not hide
    assert [f["name"] for f in r.export_results(api, SID, run, upload=fake_upload(store)).files] == []


@linux_only
def test_a_file_that_failed_to_save_is_tried_again(tmp_path):
    run = _run_for(tmp_path)
    _put(run, "a.bin", b"aaa")
    calls = {"n": 0}

    def flaky(adapter, sid, path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise rv.V2Error("upload_failed", "boom")
        return {"size": 3, "sha256": sha(b"aaa"), "file_id": "f1"}

    api = FakeApi()
    first = r.export_results(api, SID, run, upload=flaky)
    assert first.failed and first.files == []
    second = r.export_results(api, SID, run, upload=flaky)
    assert not second.failed and [f["name"] for f in second.files] == ["a.bin"]


@linux_only
def test_the_automatic_save_makes_no_call_when_nothing_changed(tmp_path):
    run = _run_for(tmp_path)
    api = FakeApi()
    out = r.export_results(api, SID, run, upload=fake_upload([]), quiet_if_unchanged=True)
    assert api.calls == [] and out.files == []  # an empty results folder: not even export-start
    _put(run, "a.bin", b"aaa")
    r.export_results(api, SID, run, upload=fake_upload([]), quiet_if_unchanged=True)
    calls = len(api.calls)
    assert calls >= 2  # export-start and export-result
    r.export_results(api, SID, run, upload=fake_upload([]), quiet_if_unchanged=True)
    assert len(api.calls) == calls  # unchanged since: silent


@linux_only
def test_an_empty_file_does_not_keep_the_automatic_save_busy(tmp_path):
    run = _run_for(tmp_path)
    _put(run, "empty.txt", b"")
    api = FakeApi()
    r.export_results(api, SID, run, upload=fake_upload([]), quiet_if_unchanged=True)
    calls = len(api.calls)
    r.export_results(api, SID, run, upload=fake_upload([]), quiet_if_unchanged=True)
    assert len(api.calls) == calls


# ---- the running rental saves by itself ---------------------------------------------------------------------------------
@linux_only
def test_a_running_rental_saves_its_results_on_a_timer_without_being_asked(tmp_path):
    store = []
    api = FakeApi(**{"/heartbeat": _beats(12)})  # the customer never presses the button
    runner = Runner(on_run=_plant({"model.pt": b"weights", "logs/run.txt": b"log"}))
    ag = agent_for(tmp_path, api, runner, upload=fake_upload(store), autosave_seconds=0.001)
    assert run_serve(ag, _claim()) == "stop"
    paths = api.paths()
    assert paths.index(f"/{SID}/export-result") < paths.index(f"/{SID}/ended")  # saved while the container was still running
    assert sorted(n for n, _ in store) == ["logs/run.txt", "model.pt"]  # each file sent exactly once in total
    assert os.listdir(tmp_path / "rental") == []


@linux_only
def test_the_automatic_save_is_off_when_set_to_zero(tmp_path):
    store = []
    api = FakeApi(**{"/heartbeat": _beats(6)})
    runner = Runner(on_run=_plant({"model.pt": b"weights"}))
    ag = agent_for(tmp_path, api, runner, upload=fake_upload(store), autosave_seconds=0)
    run_serve(ag, _claim())
    paths = api.paths()
    assert paths.index(f"/{SID}/export-result") > paths.index(f"/{SID}/ended")  # only the end-of-session save


# ---- miners by command line ---------------------------------------------------------------------------------------------
@linux_only
@pytest.mark.parametrize("line", [
    "python3 python3 run.py --pool stratum+tcp://pool.example:3333 --user x",
    "node /usr/bin/node app.js STRATUM+SSL://pool.example:443",
    "sh sh -c ./go --url=stratum2+tcp://a:1",
])
@linux_only
def test_a_pool_address_on_the_command_line_is_a_miner(line):
    assert r.detect_miner("COMMAND COMMAND\nbash bash\n" + line + "\n") in r.MINER_ARG_MARKERS


@linux_only
def test_ordinary_command_lines_are_not_miners():
    fine = "COMMAND COMMAND\npython3 python3 train.py --epochs 3 --data /workspace/data\njupyter-lab /opt/venv/bin/jupyter-lab --no-browser\nsleep sleep 30\n"
    assert r.detect_miner(fine) is None
    assert r.detect_miner("COMMAND COMMAND\nxmrig xmrig -o pool\n") == "xmrig"  # the process name still wins
    assert r.detect_miner("python3 python3 notes about stratum layers in geology\n") is None


@linux_only
def test_the_monitor_asks_docker_for_the_command_line_too(tmp_path):
    runner = Runner(top="COMMAND COMMAND\nbash bash\n")
    ag = agent_for(tmp_path, FakeApi(**{"/heartbeat": _beats(3)}), runner)
    run_serve(ag, _claim())
    tops = runner.ran("docker", "top")
    assert tops and all(c[-1] == "comm,args" for c in tops)


# ---- downloads that continue ----------------------------------------------------------------------------------------------
class Flaky:
    """A store that serves ``body`` but drops the connection after ``cut`` bytes on the first ``drops`` requests. ``ranges`` False: it
    ignores Range and always sends the whole body (status 200)."""

    def __init__(self, body: bytes, *, cut: int, drops: int = 1, ranges: bool = True):
        self.body, self.cut, self.drops, self.ranges = body, cut, drops, ranges
        self.requests: list[str | None] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            rng = request.headers.get("range")
            self.requests.append(rng)
            start = 0
            status = 200
            if rng and self.ranges:
                start = int(rng.split("=")[1].rstrip("-"))
                status = 206
            data = self.body[start:]
            headers = {"content-range": f"bytes {start}-{len(self.body) - 1}/{len(self.body)}"} if status == 206 else {}
            if len(self.requests) <= self.drops:
                def broken():
                    yield data[: self.cut]
                    raise httpx.ReadError("connection dropped")

                return httpx.Response(status, headers=headers, stream=_Stream(broken()))
            return httpx.Response(status, headers=headers, content=data)

        return httpx.MockTransport(handler)


class _Stream(httpx.SyncByteStream):
    def __init__(self, it):
        self.it = it

    def __iter__(self):
        return self.it


MIB = 1024 * 1024  # downloads are handed over in 1 MiB pieces, so a drop resumes from the last whole piece


def test_a_dropped_download_continues_from_where_it_stopped(tmp_path):
    body = os.urandom(3 * MIB + MIB // 2)
    f = Flaky(body, cut=2 * MIB + MIB // 4)
    dest = str(tmp_path / "x.bin")
    n = rv.download_verified("https://store.example/x", dest, sha256=sha(body), size=len(body), transport=f.transport(), sleeper=lambda s: None)
    assert n == len(body) and open(dest, "rb").read() == body
    assert f.requests == [None, f"bytes={2 * MIB}-"]  # the second request asked only for the rest
    assert not os.path.exists(dest + ".part")


def test_several_drops_in_a_row_still_finish(tmp_path):
    body = os.urandom(4 * MIB)
    f = Flaky(body, cut=MIB + MIB // 2, drops=2)
    dest = str(tmp_path / "x.bin")
    rv.download_verified("https://store.example/x", dest, sha256=sha(body), size=len(body), transport=f.transport(), sleeper=lambda s: None)
    assert open(dest, "rb").read() == body and f.requests == [None, f"bytes={MIB}-", f"bytes={2 * MIB}-"]


def test_a_server_that_ignores_the_range_makes_it_start_again_cleanly(tmp_path):
    body = os.urandom(3 * MIB)
    f = Flaky(body, cut=MIB + MIB // 2, ranges=False)
    dest = str(tmp_path / "x.bin")
    rv.download_verified("https://store.example/x", dest, sha256=sha(body), size=len(body), transport=f.transport(), sleeper=lambda s: None)
    assert open(dest, "rb").read() == body  # no duplicated bytes, hash still right


def test_a_wrong_hash_after_resuming_is_still_refused_and_nothing_is_left(tmp_path):
    body = os.urandom(3 * MIB)
    f = Flaky(body, cut=MIB + MIB // 2)
    dest = str(tmp_path / "x.bin")
    with pytest.raises(rv.V2Error) as e:
        rv.download_verified("https://store.example/x", dest, sha256="0" * 64, size=len(body), transport=f.transport(), sleeper=lambda s: None)
    assert e.value.code == "input_hash_mismatch" and not os.path.exists(dest) and not os.path.exists(dest + ".part")


def test_giving_up_leaves_no_partial_file(tmp_path):
    body = os.urandom(100_000)
    f = Flaky(body, cut=10_000, drops=99)
    dest = str(tmp_path / "x.bin")
    with pytest.raises(rv.V2Error) as e:
        rv.download_verified("https://store.example/x", dest, sha256=sha(body), size=len(body), transport=f.transport(), retries=3, sleeper=lambda s: None)
    assert e.value.code == "download_failed" and not os.path.exists(dest) and not os.path.exists(dest + ".part")


def test_a_part_file_from_an_earlier_run_is_never_trusted(tmp_path):
    body = b"the real bytes" * 1000
    dest = str(tmp_path / "x.bin")
    open(dest + ".part", "wb").write(b"planted garbage")
    seen = []

    def handler(request):
        seen.append(request.headers.get("range"))
        return httpx.Response(200, content=body)

    rv.download_verified("https://store.example/x", dest, sha256=sha(body), size=len(body), transport=httpx.MockTransport(handler))
    assert seen == [None] and open(dest, "rb").read() == body


def test_more_bytes_than_declared_is_refused(tmp_path):
    body = b"x" * 1000
    dest = str(tmp_path / "x.bin")
    with pytest.raises(rv.V2Error) as e:
        rv.download_verified("https://store.example/x", dest, sha256=sha(body), size=500,
                             transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body)))
    assert e.value.code == "input_hash_mismatch" and not os.path.exists(dest + ".part")
