"""U18 private Kit jobs, sealed stream pipe and padding, U14 VM wiring. Written, NOT run in the authoring session."""

from __future__ import annotations

import base64
import os
import stat

import pytest

from computecoin_agent import private_kit as pk
from computecoin_agent import runtime_v2 as rv2
from computecoin_agent.executor import JobSpec
from computecoin_agent.jobmonitor import JobMonitor
from computecoin_agent.privacy_crypto import generate_x25519_keypair, open_sealed, stream_aad


# ---------------------------------------------------------------- minimal private plan (fail closed)
def _plan(**extra):
    return {"spec_version": 2, "private": True, "kit": {"slug": "demo", "version": "1.0.0"},
            "outputs": [{"name": "result", "path": "result.bin", "required": True}], **extra}


def test_private_plan_accepts_minimal_block():
    plan = pk.parse_private_plan(_plan())
    assert plan.kit == "demo" and plan.outputs[0]["name"] == "result"


@pytest.mark.parametrize("extra", [{"inputs": []}, {"assets": []}, {"egress": ["a.b:443"]}, {"checkpoint": None}, {"vm": {}}, {"mode": "vm"}])
def test_private_plan_refuses_any_extra_field(extra):
    with pytest.raises(pk.PrivateKitError):
        pk.parse_private_plan(_plan(**extra))


def test_private_plan_refuses_non_private_block_and_bad_outputs():
    with pytest.raises(pk.PrivateKitError):
        pk.parse_private_plan({"kit": {"slug": "demo"}, "outputs": []})
    bad = _plan()
    bad["outputs"] = [{"name": "x", "path": "../escape"}]
    with pytest.raises(pk.PrivateKitError):
        pk.parse_private_plan(bad)


# ---------------------------------------------------------------- RAM-only work dir
def test_ram_workdir_refuses_non_tmpfs(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/sda1 / ext4 rw 0 0\n/dev/sda1 {tmp_path} ext4 rw 0 0\n")
    with pytest.raises(pk.PrivateKitError) as exc:
        pk.ram_workdir("t-", str(tmp_path), mounts_file=str(mounts))
    assert exc.value.code == "ram_workdir_unavailable"


def test_ram_workdir_accepts_tmpfs(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text(f"tmpfs {tmp_path} tmpfs rw 0 0\n")
    d = pk.ram_workdir("t-", str(tmp_path), mounts_file=str(mounts))
    try:
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    finally:
        pk.wipe_dir(d)
    assert not os.path.exists(d)


def test_stage_inputs_and_wipe(tmp_path):
    params, names = pk.stage_private_inputs(
        {"inputs": {"a.txt": {"text": "secret"}, "b.bin": {"b64": base64.b64encode(b"\x01\x02").decode()}}, "params": {"k": 1}},
        str(tmp_path / "in"))
    assert params == {"k": 1} and sorted(names) == ["a.txt", "b.bin"]
    assert (tmp_path / "in" / "a.txt").read_bytes() == b"secret"
    pk.wipe_dir(str(tmp_path), keep_root=True)
    assert list(tmp_path.iterdir()) == []


def test_stage_inputs_refuses_bad_names_and_plain_dict_is_params(tmp_path):
    with pytest.raises(pk.PrivateKitError):
        pk.stage_private_inputs({"inputs": {"../x": {"text": "a"}}}, str(tmp_path / "in"))
    params, names = pk.stage_private_inputs({"n": 3}, str(tmp_path / "in2"))
    assert params == {"n": 3} and names == []


# ---------------------------------------------------------------- private Kit run: no network, sealed outputs, nothing plaintext uploaded
def test_private_kit_run_seals_outputs_and_wipes(tmp_path, monkeypatch):
    priv, pub = generate_x25519_keypair()
    uploaded: list[bytes] = []

    def fake_upload(api, job_id, path, *, purpose, name, **kw):
        data = open(path, "rb").read()
        uploaded.append(data)
        import hashlib
        return {"kind": purpose, "name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "file_id": "f1"}

    monkeypatch.setattr(rv2, "upload_file", fake_upload)
    seen = {}

    class FakeExec:
        def run(self, sub):
            seen["spec"] = sub
            with open(os.path.join(sub.output_dir, "result.bin"), "wb") as fh:
                fh.write(b"PLAINTEXT-RESULT")
            from computecoin_agent.executor import ExecutionResult
            return ExecutionResult(status="success", execution_mode="hardware", metrics={"duration_seconds": 1.0})

    spec = JobSpec(job_id="00000000-0000-4000-8000-000000000001", workload_type="kit", model="demo@1", input={},
                   execution_mode="hardware", image_digest="r/i@sha256:" + "a" * 64, isolation_tier="sealed",
                   result_public_key=pub)
    ctx = pk.PrivateKitContext(pk.parse_private_plan(_plan()), str(tmp_path / "ram"), api=None)
    os.makedirs(ctx.ram_dir)
    out = pk.run_private_kit(spec, {"inputs": {"in.txt": {"text": "hi"}}, "params": {}}, ctx, lambda s, c: FakeExec(), None)

    sub = seen["spec"]
    assert sub.private_kit and sub.network_policy is None and sub.isolation_tier == "sealed"
    assert sub.mounts[0]["target"] == "/in/in.txt" and sub.mounts[0]["readonly"] is True
    assert uploaded and all(b"PLAINTEXT-RESULT" not in u for u in uploaded)  # only ciphertext left the host
    entry = out["outputs"][0]
    import json
    env = json.loads(uploaded[0])
    assert open_sealed(priv, env, entry["aad"].encode()) == b"PLAINTEXT-RESULT"
    assert os.listdir(ctx.ram_dir) == []  # work dir wiped


def test_private_kit_refuses_network_and_simulated(tmp_path):
    spec = JobSpec(job_id="j", workload_type="kit", model="m", input={}, execution_mode="simulated",
                   image_digest="x", isolation_tier="sealed", result_public_key="00" * 32)
    ctx = pk.PrivateKitContext(pk.parse_private_plan(_plan()), str(tmp_path), api=None)
    with pytest.raises(pk.PrivateKitError):
        pk.run_private_kit(spec, {}, ctx, lambda s, c: None, None)


def test_executor_allows_private_mounts_only_for_private_kit():
    from computecoin_agent.executor import DockerExecutor, ExecutorError

    base = dict(job_id="j", workload_type="kit", model="m", input={}, execution_mode="hardware", image_digest="r/i@sha256:" + "b" * 64,
                isolation_tier="sealed", mounts=[{"source": "/dev/shm/x", "target": "/in/x", "readonly": True}])
    with pytest.raises(ExecutorError):
        DockerExecutor().build_argv(JobSpec(**base), "/dev/shm/out")
    argv = DockerExecutor().build_argv(JobSpec(**base, private_kit=True), "/dev/shm/out")
    assert argv[argv.index("--network") + 1] == "none"


# ---------------------------------------------------------------- sealed stream: padded, pipe not file
def test_padding_frame_is_block_aligned_and_reversible():
    framed = pk.pad_stream_plaintext(b"hello", 256)
    assert len(framed) == 256 and int.from_bytes(framed[:4], "big") == 5 and framed[4:9] == b"hello"
    assert pk.pad_stream_plaintext(b"hello", 0) == b"hello"
    assert len(pk.pad_stream_plaintext(b"x" * 300, 256)) == 512


def test_sealed_monitor_uses_fifo_and_posts_padded_ciphertext(tmp_path):
    priv, pub = generate_x25519_keypair()
    posted = []
    mon = JobMonitor("http://x", "m", "s", "job-1", stream=True, seal_to=pub, pad_bytes=256)
    mon.set_output_dir(str(tmp_path))
    path = tmp_path / "stream.ndjson"
    assert stat.S_ISFIFO(os.stat(path).st_mode)
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, b'{"text": "hello", "final": true}\n')
    os.close(fd)

    class R:
        status_code = 200

    import computecoin_agent.jobrunner as jr
    orig = jr._signed_post
    jr._signed_post = lambda url, mid, sec, body, **kw: (posted.append(body), R())[1]
    try:
        mon._pump_stream(final=True)
    finally:
        jr._signed_post = orig
        mon.stop()
    body = posted[0]
    assert body["text"] == "" and body["final"] is True
    assert (len(body["sealed"]["ct"]) // 2 - 16) % 256 == 0
    plain = open_sealed(priv, body["sealed"], stream_aad("job-1", 0, True))
    assert plain[4:4 + int.from_bytes(plain[:4], "big")] == b"hello"


# ---------------------------------------------------------------- U14 VM wiring fails closed
class _FakeVm:
    class VmJob:  # noqa: D106
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def __init__(self, vfio=True):
        self._vfio = vfio

    def advertise(self):
        return {"vfio_gpu": self._vfio}

    def detect_vfio_gpu(self):
        return ["0000:01:00.0"]


def _vm_spec(passthrough=True):
    v2 = {"mode": "vm", "kit": {"slug": "demo"}, "outputs": [], "vm": {
        "image": {"name": "vm-image", "file_id": "f", "sha256": "a" * 64, "size_bytes": 10},
        "vcpus": 4, "memory_gb": 16, "gpu_passthrough": passthrough, "params": {}}}
    return JobSpec(job_id="j" * 8, workload_type="kit", model="m", input={}, execution_mode="hardware", v2=v2)


def test_vm_job_refused_when_disabled_or_no_vfio_or_private():
    ctx = rv2.V2Context(api=None, cache=None)  # never reached on the refusal paths
    assert rv2.run_vm_v2(_vm_spec(), ctx, windows_vm_enabled=False)[0].error["code"] == "windows_vm_disabled"
    assert rv2.run_vm_v2(_vm_spec(), ctx, windows_vm_enabled=True, vm_module=_FakeVm(vfio=False))[0].error["code"] == "vfio_gpu_required"
    priv = _vm_spec()
    priv.isolation_tier = "sealed"
    assert rv2.run_vm_v2(priv, ctx, windows_vm_enabled=True, vm_module=_FakeVm())[0].error["code"] == "v2_not_supported_for_privacy_tier"
    nov = _vm_spec()
    nov.v2["vm"] = None
    assert rv2.run_vm_v2(nov, ctx, windows_vm_enabled=True, vm_module=_FakeVm())[0].error["code"] == "vm_spec_unavailable"
