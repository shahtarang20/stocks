"""U14 vm_runner tests. Written without being run in the authoring session; no QEMU or KVM is needed."""

import pytest

from computecoin_agent import vm_runner as vm


def _job(**kw):
    return vm.VmJob(job_id="abcdef0123456789", image_path="/x/win.img", image_sha256="0" * 64, input_dir=None, **kw)


def _argv(job=None):
    return vm.build_qemu_command(job or _job(gpu_addresses=["0000:01:00.0"]), overlay="/s/o.qcow2",
                                 input_img="/s/i.img", output_img="/s/o.img", control_sock="/s/ctl.sock")


def test_command_has_no_network_and_readonly_input():
    a = _argv()
    assert a[a.index("-nic") + 1] == "none"
    assert a[a.index("-monitor") + 1] == "none"
    assert "vfio-pci,host=0000:01:00.0" in a
    drives = [a[i + 1] for i, x in enumerate(a) if x == "-drive"]
    assert "readonly=on" in drives[1] and "readonly=on" not in drives[2]


@pytest.mark.parametrize("extra", [["-netdev", "user,id=n"], ["-nic", "user"], ["-virtfs", "x"], ["-vnc", ":1"], ["-qmp", "tcp:0:4444"]])
def test_assert_isolated_rejects_network_and_consoles(extra):
    a = _argv() + extra
    with pytest.raises(vm.VmError):
        vm.assert_isolated(a)


def test_vfio_device_needs_address():
    a = _argv() + ["-device", "vfio-pci"]
    with pytest.raises(vm.VmError):
        vm.assert_isolated(a)


def test_no_gpu_means_no_vfio_device():
    assert not any(x.startswith("vfio-pci") for x in _argv(_job()))


def test_detect_vfio_gpu_needs_everything(tmp_path):
    assert vm.detect_vfio_gpu(str(tmp_path / "sys"), str(tmp_path / "dev")) == []
    (tmp_path / "dev/vfio").mkdir(parents=True)
    (tmp_path / "dev/vfio/vfio").write_text("")
    (tmp_path / "dev/kvm").write_text("")
    (tmp_path / "sys/kernel/iommu_groups/1").mkdir(parents=True)
    (tmp_path / "sys/bus/pci/drivers/vfio-pci/0000:01:00.0").mkdir(parents=True)
    d = tmp_path / "sys/bus/pci/devices/0000:01:00.0"
    d.mkdir(parents=True)
    (d / "class").write_text("0x030000\n")
    assert vm.detect_vfio_gpu(str(tmp_path / "sys"), str(tmp_path / "dev")) == ["0000:01:00.0"]
    (d / "class").write_text("0x020000\n")  # a NIC is not a GPU
    assert vm.detect_vfio_gpu(str(tmp_path / "sys"), str(tmp_path / "dev")) == []


def test_advertise_false_without_tools():
    assert vm.advertise(which=lambda _n: None) == {"vfio_gpu": False}


def test_run_refuses_wrong_image_hash(tmp_path):
    img = tmp_path / "win.img"
    img.write_bytes(b"not windows")
    res = vm.run_vm_job(vm.VmJob(job_id="j" * 16, image_path=str(img), image_sha256="1" * 64, input_dir=None),
                        work_root=str(tmp_path), out_dir=str(tmp_path / "out"))
    assert res.status == "failed" and res.error_code == "vm_image_hash_mismatch"
