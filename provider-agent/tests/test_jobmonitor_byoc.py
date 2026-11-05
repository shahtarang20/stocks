"""U8/U16: BYOC container sampling in JobMonitor (process names + I/O only). WRITTEN, NOT YET RUN."""

from __future__ import annotations

from computecoin_agent import jobmonitor as jm


def test_parse_docker_top_names_only_capped_unique():
    out = "COMMAND\npython3\n/usr/local/bin/xmrig\npython3\n" + "\n".join(f"p{i}" for i in range(200))
    names = jm.parse_docker_top(out)
    assert names[:2] == ["python3", "xmrig"] and len(names) == jm.BYOC_MAX_PROCESSES


def test_parse_docker_stats_io_sums_net_and_block():
    assert jm.parse_docker_stats_io("1.5kB / 2kB|1MB / 0B\n") == 1500 + 2000 + 1_000_000
    assert jm.parse_docker_stats_io("") is None


def _monitor(runner, container="ccoin-job-1"):
    m = jm.JobMonitor("http://x", "m", "s", "11111111-1111-1111-1111-111111111111",
                      gpu_sampler=lambda: {"gpu_util_pct": 99.0, "power_w": 250.0},
                      byoc_container=container, docker_runner=runner)
    sent = {}
    m._reporter.report = lambda job_id, pct, **kw: sent.update(kw)  # capture instead of POSTing
    return m, sent


def test_byoc_report_carries_names_io_and_power_additively():
    def runner(argv):
        assert argv[0] == "docker" and isinstance(argv, list)
        return "COMMAND\nxmrig\n" if argv[1] == "top" else "0B / 0B|0B / 0B"
    m, sent = _monitor(runner)
    m._report_progress()
    assert sent["process_names"] == ["xmrig"] and sent["io_bytes"] == 0 and sent["power_w"] == 250.0


def test_non_byoc_reports_no_extra_fields_and_never_calls_docker():
    def runner(argv):
        raise AssertionError("docker must not be called")
    m, sent = _monitor(runner, container=None)
    m._report_progress()
    assert "process_names" not in sent and "io_bytes" not in sent and sent["power_w"] is None


def test_byoc_sampling_is_rate_limited_and_failure_is_silent():
    calls = []
    m, sent = _monitor(lambda argv: calls.append(argv) or None)
    m._report_progress()
    n = len(calls)
    m._report_progress()
    assert len(calls) == n  # second tick inside BYOC_SAMPLE_SECONDS does not touch docker


# --- review fix: stream/progress files are written by the untrusted job ---------------------------------------------
def _plain_monitor(tmp_path):
    m = jm.JobMonitor("http://x", "m", "s", "11111111-1111-1111-1111-111111111111", gpu_sampler=lambda: {})
    m.set_output_dir(str(tmp_path))
    return m


def test_stream_file_symlink_to_host_file_is_not_read(tmp_path):
    host = tmp_path / "host.ndjson"
    host.write_text('{"text": "HOST SECRET"}\n')
    out = tmp_path / "out"
    out.mkdir()
    (out / jm.STREAM_FILE).symlink_to(host)
    m = _plain_monitor(out)
    assert m._read_new_lines() == []


def test_stream_file_is_read_in_bounded_pieces_and_runaway_line_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "MAX_READ_BYTES", 64)
    (tmp_path / jm.STREAM_FILE).write_bytes(b"x" * 500 + b'\n{"text": "ok"}\n')
    m = _plain_monitor(tmp_path)
    seen = []
    for _ in range(20):
        seen += m._read_new_lines()
    assert seen == [{"text": "ok"}]


def test_progress_file_symlink_is_ignored(tmp_path):
    host = tmp_path / "host.json"
    host.write_text('{"progress_pct": 77}')
    out = tmp_path / "out"
    out.mkdir()
    (out / jm.PROGRESS_FILE).symlink_to(host)
    m = _plain_monitor(out)
    sent = {}
    m._reporter.report = lambda job_id, pct, **kw: sent.update(pct=pct)
    m._report_progress()
    assert sent["pct"] == 0.0


def test_nan_progress_from_the_job_is_not_reported_as_complete(tmp_path):
    """json.loads accepts NaN; min(NaN, 99) is NaN and the reporter's clamp then turned it into 100 %."""
    (tmp_path / "progress.json").write_text('{"progress_pct": NaN, "phase": "x"}')
    m = _plain_monitor(tmp_path)
    got = {}
    m._reporter.report = lambda job_id, pct, **kw: got.update(pct=pct)
    m._report_progress()
    assert got["pct"] == 0.0
