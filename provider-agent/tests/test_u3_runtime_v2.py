"""U3 agent runtime v2 (written, NOT executed): argv, cache, staging, checkpoints, outputs."""

import hashlib
import os
import tarfile

import pytest

from computecoin_agent import runtime_v2 as rv
from computecoin_agent.executor import DockerExecutor, ExecutionResult, ExecutorError, JobSpec

DIGEST = "img@sha256:" + "a" * 64
PROXY = "proxy@sha256:" + "b" * 64


def _spec(**kw):
    base = dict(job_id="j1", workload_type="text-generation", model="m", input={}, execution_mode="hardware", image_digest=DIGEST)
    base.update(kw)
    return JobSpec(**base)


def _ex():
    return DockerExecutor(docker_available=lambda: True, egress_proxy_image=PROXY)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- U3.1: read-only mounts, ADR-0003 flags stay --------------------------------
def test_mounts_are_readonly_and_hardening_stays():
    spec = _spec(mounts=[{"source": "/data/x", "target": "/in/x", "readonly": True}], extra_env={"COMPUTECOIN_INPUTS_DIR": "/in"})
    argv = _ex().build_argv(spec, "/tmp/out")
    assert "type=bind,source=/data/x,target=/in/x,readonly" in argv
    for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--user"):
        assert flag in argv
    assert "COMPUTECOIN_INPUTS_DIR=/in" in argv


def test_v1_argv_unchanged_without_v2():
    a = _ex().build_argv(_spec(), "/tmp/out")
    assert not any("readonly" in x for x in a)


@pytest.mark.parametrize("target", ["/", "/etc/x", "/out/x", "/tmp", "rel/x", "/a/../b", "/a,b", "/proc/1"])
def test_bad_mount_targets_refused(target):
    with pytest.raises(ExecutorError):
        _ex().build_argv(_spec(mounts=[{"source": "/data/x", "target": target}]), "/tmp/out")


def test_mount_source_with_comma_refused():
    with pytest.raises(ExecutorError):
        _ex().build_argv(_spec(mounts=[{"source": "/data/x,source=/", "target": "/in/x"}]), "/tmp/out")


def test_privacy_tier_never_gets_v2_mounts_or_egress():
    with pytest.raises(ExecutorError):
        _ex().build_argv(_spec(isolation_tier="sealed", mounts=[{"source": "/d", "target": "/in/x"}]), "/tmp/out")
    result, desc = rv.run_v2(_spec(isolation_tier="confidential", v2={"kit": {"slug": "k"}}), rv.V2Context(api=None, cache=rv.ContentCache("/nonexistent", 0)), lambda s: None)
    assert result.status == "failed" and desc == []


# --- U3.3: kit egress reuses the B5 argv ----------------------------------------
def test_kit_egress_goes_through_internal_network_and_proxy():
    argv = _ex().build_argv(_spec(network_policy={"egress": ["huggingface.co:443"]}), "/tmp/out")
    assert argv[argv.index("--network") + 1] == "ccoin-net-j1"


def test_plan_rejects_wildcard_egress():
    with pytest.raises(rv.V2Error):
        rv.parse_plan({"kit": {"slug": "k"}, "egress": ["*.evil.com:443"]}, min_interval=60)


# --- U3.2: cache ----------------------------------------------------------------
def _put(cache, kit, data, tmp_path, name):
    src = tmp_path / ("src_" + name)  # not "c": that is the cache directory
    src.write_bytes(data)
    return cache.store(kit, sha(data), str(src), len(data))


def test_cache_hit_prevents_download(tmp_path):
    cache = rv.ContentCache(str(tmp_path / "c"), 1000)
    p = _put(cache, "kit", b"weights", tmp_path, "a")
    assert p is not None
    assert cache.lookup("kit", sha(b"weights"), 7) == p
    assert cache.downloads == 0 and cache.hits == 1


def test_cache_is_per_kit(tmp_path):
    cache = rv.ContentCache(str(tmp_path / "c"), 1000)
    _put(cache, "kit-a", b"weights", tmp_path, "a")
    assert cache.lookup("kit-b", sha(b"weights"), 7) is None


def test_lru_eviction_and_pin(tmp_path):
    cache = rv.ContentCache(str(tmp_path / "c"), 20)
    a = _put(cache, "k", b"a" * 10, tmp_path, "a")
    os.utime(a, (1, 1))
    b = _put(cache, "k", b"b" * 10, tmp_path, "b")
    os.utime(b, (2, 2))
    cache.pin(a)  # in use by a running job: not evictable even though oldest
    c = _put(cache, "k", b"c" * 10, tmp_path, "c")
    assert a.exists() and not b.exists() and c is not None


def test_object_larger_than_limit_not_cached(tmp_path):
    cache = rv.ContentCache(str(tmp_path / "c"), 5)
    assert _put(cache, "k", b"x" * 10, tmp_path, "x") is None


def test_size_mismatch_entry_is_dropped(tmp_path):
    cache = rv.ContentCache(str(tmp_path / "c"), 100)
    p = _put(cache, "k", b"abc", tmp_path, "a")
    assert cache.lookup("k", sha(b"abc"), 99) is None and not p.exists()


def test_disabled_cache_never_stores(tmp_path):
    cache = rv.ContentCache(str(tmp_path / "c"), 0)
    assert _put(cache, "k", b"abc", tmp_path, "a") is None


# --- download verification ------------------------------------------------------
def test_hash_mismatch_refused(tmp_path):
    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"evil!!"))
    dest = str(tmp_path / "f")
    with pytest.raises(rv.V2Error) as e:
        rv.download_verified("https://store.example/x", dest, sha256=sha(b"good!!"), size=6, transport=transport)
    assert e.value.code == "input_hash_mismatch" and not os.path.exists(dest) and not os.path.exists(dest + ".part")


def test_download_ok(tmp_path):
    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"good!!"))
    dest = str(tmp_path / "f")
    assert rv.download_verified("https://store.example/x", dest, sha256=sha(b"good!!"), size=6, transport=transport) == 6


# --- U3.4: checkpoints ----------------------------------------------------------
def test_pack_and_restore_round_trip(tmp_path):
    src = tmp_path / "s"
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(b"1")
    (src / "sub" / "b.bin").write_bytes(b"2")
    (src / "link").symlink_to("/etc/passwd")  # links are never packed
    tar = str(tmp_path / "c.tar")
    rv.pack_dir(str(src), tar)
    dest = tmp_path / "d"
    dest.mkdir()
    rv.safe_extract(tar, str(dest), max_bytes=1000)
    assert (dest / "sub" / "b.bin").read_bytes() == b"2" and not (dest / "link").exists()


def test_pack_is_deterministic(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (src / "a").write_bytes(b"1")
    rv.pack_dir(str(src), str(tmp_path / "1.tar"))
    rv.pack_dir(str(src), str(tmp_path / "2.tar"))
    assert rv.hash_file(str(tmp_path / "1.tar")) == rv.hash_file(str(tmp_path / "2.tar"))


@pytest.mark.parametrize("name", ["../evil", "/abs/evil"])
def test_unsafe_member_refused(tmp_path, name):
    tar = str(tmp_path / "bad.tar")
    with tarfile.open(tar, "w") as t:
        info = tarfile.TarInfo(name)
        info.size = 1
        import io

        t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(rv.V2Error):
        rv.safe_extract(tar, str(tmp_path), max_bytes=100)


def test_symlink_member_refused(tmp_path):
    tar = str(tmp_path / "bad.tar")
    with tarfile.open(tar, "w") as t:
        info = tarfile.TarInfo("l")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        t.addfile(info)
    with pytest.raises(rv.V2Error):
        rv.safe_extract(tar, str(tmp_path), max_bytes=100)


def test_seq_parse_newest_wins():
    assert rv._seq_of("ckpt/j/00000007.tar") == 7 and rv._seq_of("junk") == -1


def test_checkpoint_waits_for_commit_marker(tmp_path):
    import threading

    d = tmp_path / "ck"
    d.mkdir()
    (d / "x").write_bytes(b"1")
    up = rv.CheckpointUploader(None, "j", str(d), str(tmp_path), 60, first_seq=0, max_bytes=100, stop=threading.Event(), logs=[])
    assert up.snapshot() is False  # no COMMIT marker: never a torn snapshot


# --- outputs --------------------------------------------------------------------
def test_missing_required_output_fails(tmp_path):
    plan = rv.parse_plan({"kit": {"slug": "k"}, "outputs": [{"name": "frame", "path": "f.png", "required": True}]}, min_interval=60)
    with pytest.raises(rv.V2Error) as e:
        rv.collect_and_upload_outputs(rv.V2Context(api=None, cache=rv.ContentCache(str(tmp_path), 0)), "j", plan, str(tmp_path), str(tmp_path))
    assert e.value.code == "missing_output"


def test_output_symlink_escape_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "f.png").symlink_to("/etc/passwd")
    plan = rv.parse_plan({"kit": {"slug": "k"}, "outputs": [{"name": "frame", "path": "f.png"}]}, min_interval=60)
    with pytest.raises(rv.V2Error) as e:
        rv.collect_and_upload_outputs(rv.V2Context(api=None, cache=rv.ContentCache(str(tmp_path), 0)), "j", plan, str(out), str(tmp_path))
    assert e.value.code == "unsafe_output"


def test_checkpoint_interval_has_floor():
    plan = rv.parse_plan({"kit": {"slug": "k"}, "checkpoint": {"dir": "/ckpt", "interval_seconds": 5}}, min_interval=60)
    assert plan.checkpoint_interval == 60


def test_pack_dir_survives_file_swapped_for_symlink_while_packing(tmp_path, monkeypatch):
    """The checkpoint dir is written by a running, untrusted container: a regular file swapped for a symlink to a host
    file between the type check and the read must not put the host file's bytes into the tar."""
    secret = tmp_path / "host-secret"
    secret.write_bytes(b"HOST-SECRET-BYTES")
    src = tmp_path / "s"
    src.mkdir()
    victim = src / "ckpt.bin"
    victim.write_bytes(b"innocent")  # 8 bytes: the header size the swapped-in symlink will be read with
    swapped = []

    def swap():
        if not swapped:
            swapped.append(1)
            os.unlink(victim)
            os.symlink(secret, victim)

    real_lstat, real_open = os.lstat, os.open

    lstats = []

    def lstat(path, *a, **k):
        r = real_lstat(path, *a, **k)
        if str(path).endswith("ckpt.bin"):
            lstats.append(1)
            if len(lstats) >= 2:  # after the type check AND the tar header stat, right before the read
                swap()
        return r

    def opener(path, *a, **k):
        if str(path).endswith("ckpt.bin"):
            swap()
        return real_open(path, *a, **k)

    monkeypatch.setattr(rv.os, "lstat", lstat)
    monkeypatch.setattr(rv.os, "open", opener)
    tar = str(tmp_path / "c.tar")
    rv.pack_dir(str(src), tar)
    assert b"HOST-SEC" not in open(tar, "rb").read()


def test_asset_and_input_with_the_same_name_do_not_overwrite_each_other(tmp_path):
    """With the cache off, an asset and an input named alike were staged to the same file: one mount served the other's bytes."""
    import httpx

    blobs = {"asset": b"KIT-ASSET", "input": b"CUSTOMER-INPUT"}

    class Api:
        def ok(self, path, body=None, *, code):
            return {"url": "https://store.example/" + body["kind"]}

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=blobs[req.url.path.lstrip("/")]))
    ctx = rv.V2Context(api=Api(), cache=rv.ContentCache(str(tmp_path / "cache"), 0), transport=transport)
    plan = rv.Plan(
        "k",
        [{"name": "model.bin", "sha256": sha(blobs["input"]), "size_bytes": len(blobs["input"]), "cacheable": False}],
        [{"name": "model.bin", "sha256": sha(blobs["asset"]), "size_bytes": len(blobs["asset"]), "cacheable": True}],
        [], [], None, 0, None,
    )
    jobdir = tmp_path / "job"
    (jobdir / "in").mkdir(parents=True)
    mounts = rv.stage_items(ctx, "j1", plan, str(jobdir), [], [])
    by_target = {m["target"]: open(m["source"], "rb").read() for m in mounts}
    assert by_target == {"/cache/model.bin": blobs["asset"], "/in/model.bin": blobs["input"]}


def test_api_network_error_while_staging_is_a_failed_job_not_an_escaping_exception(tmp_path):
    """A transport error talking to the API used to propagate out of run_v2 (only V2Error was handled), so the claimed job
    was never reported and sat RUNNING until its lease expired."""
    import httpx

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    api = rv.V2Api("https://api.test", "mch", "msk_" + "c" * 32, transport=httpx.MockTransport(boom))
    with pytest.raises(rv.V2Error) as err:
        api.post("/jobs/j1/download-url", {"kind": "input", "name": "x"})
    assert err.value.code == "api_unreachable"

    ctx = rv.V2Context(api=api, cache=rv.ContentCache(str(tmp_path / "cache"), 0), work_dir=str(tmp_path / "work"))
    v2 = {"kit": {"slug": "k"}, "inputs": [{"name": "x", "sha256": "a" * 64, "size_bytes": 1}]}
    res, descriptors = rv.run_v2(_spec(v2=v2), ctx, lambda s: pytest.fail("must not run"))
    assert res.status == "failed" and res.error["code"] == "api_unreachable" and descriptors == []
    assert not any((tmp_path / "work").iterdir())  # the job dir was removed
