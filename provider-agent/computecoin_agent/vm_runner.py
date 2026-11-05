"""Windows VM runner (U14, docs/architecture/U14-windows-vm-design.md). DEFAULT OFF: the server only schedules a VM job
when WINDOWS_VM_ENABLED=true, and a host only receives one when it advertises ``vfio_gpu``.

One job = one QEMU/KVM guest started by the agent, from a CUSTOMER-SUPPLIED licensed Windows image (the platform
ships none; the licence is the customer's responsibility). The guest has:

* no network at all (``-nic none``; no user-mode NAT, no bridge, no tap, no port forward),
* the customer image behind a throw-away copy-on-write overlay (the staged image is never modified),
* an INPUT disk attached read-only and an OUTPUT disk attached read-write (FAT32, built and read WITHOUT mounting,
  using mtools, so the host never parses guest-controlled filesystem metadata in the kernel),
* a virtio-serial control channel on a unix socket inside a 0700 directory (host-only; no TCP, no monitor),
* the GPU passed through with VFIO (only devices already bound to ``vfio-pci``),
* QEMU's seccomp sandbox on.

The argv builder is pure and ``assert_isolated`` re-checks it, so a future edit cannot quietly add a network or a
shared folder. Nothing here has been run against real hardware (see the design note, "Unverified").
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

MAX_CONTROL_BYTES = 64 * 1024
MAX_OUTPUT_FILES = 10_000
FAT32_MAX_FILE = 4 * 1024**3 - 1
_GPU_CLASS_PREFIX = "0x03"  # PCI display controller


class VmError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message or code


# --------------------------------------------------------------------------- host capability
def detect_vfio_gpu(sys_root: str = "/sys", dev_root: str = "/dev") -> list[str]:
    """PCI addresses of display devices bound to ``vfio-pci`` (empty = this host must NOT advertise ``vfio_gpu``)."""
    if not os.path.exists(os.path.join(dev_root, "vfio", "vfio")) or not os.path.exists(os.path.join(dev_root, "kvm")):
        return []
    groups = os.path.join(sys_root, "kernel", "iommu_groups")
    if not os.path.isdir(groups) or not os.listdir(groups):
        return []
    drv = os.path.join(sys_root, "bus", "pci", "drivers", "vfio-pci")
    found: list[str] = []
    try:
        names = os.listdir(drv)
    except OSError:
        return []
    for name in sorted(names):
        if ":" not in name:
            continue
        try:
            cls = Path(sys_root, "bus", "pci", "devices", name, "class").read_text().strip()
        except OSError:
            continue
        if cls.startswith(_GPU_CLASS_PREFIX):
            found.append(name)
    return found


def advertise(which: Callable[[str], Optional[str]] = shutil.which, **kw: Any) -> dict[str, Any]:
    """What a host reports in its capability snapshot: ``vfio_gpu`` is true only with qemu, mtools and a bound GPU."""
    ok = bool(which("qemu-system-x86_64") and which("qemu-img") and which("mformat") and detect_vfio_gpu(**kw))
    return {"vfio_gpu": ok}


# --------------------------------------------------------------------------- command
@dataclass
class VmJob:
    job_id: str
    image_path: str
    image_sha256: str
    input_dir: Optional[str]
    vcpus: int = 4
    memory_gb: int = 16
    gpu_addresses: list[str] = field(default_factory=list)  # [] = no passthrough
    input_disk_mb: int = 1024
    output_disk_mb: int = 2048
    timeout_seconds: int = 3600
    control_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VmResult:
    status: str  # success | failed | timeout
    output_dir: Optional[str] = None
    progress: list[str] = field(default_factory=list)
    error_code: Optional[str] = None
    duration_seconds: float = 0.0


def build_qemu_command(
    job: VmJob, *, overlay: str, input_img: str, output_img: str, control_sock: str, qemu_bin: str = "qemu-system-x86_64"
) -> list[str]:
    argv = [
        qemu_bin, "-name", f"cc-{job.job_id[:8]}", "-machine", "q35,accel=kvm", "-enable-kvm", "-cpu", "host",
        "-smp", str(int(job.vcpus)), "-m", f"{int(job.memory_gb)}G", "-nodefaults", "-no-user-config", "-nographic",
        "-display", "none", "-monitor", "none", "-serial", "none", "-parallel", "none", "-no-reboot",
        "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        "-nic", "none",
        "-drive", f"file={overlay},if=virtio,format=qcow2,cache=unsafe,discard=unmap",
        "-drive", f"file={input_img},if=virtio,format=raw,readonly=on",
        "-drive", f"file={output_img},if=virtio,format=raw",
        "-device", "virtio-serial-pci",
        "-chardev", f"socket,id=ctl,path={control_sock},server=on,wait=off",
        "-device", "virtserialport,chardev=ctl,name=computecoin.ctl",
    ]
    for addr in job.gpu_addresses:
        argv += ["-device", f"vfio-pci,host={addr}"]
    assert_isolated(argv)
    return argv


_FORBIDDEN = ("hostfwd", "tap", "bridge", "user,", "virtfs", "9p", "-net", "-netdev", "tcp:", "telnet", "-usb", "-incoming", "-qmp", "vnc", "spice")


def assert_isolated(argv: list[str]) -> None:
    """Raise VmError if the command could give the guest a network, a shared folder or a host-reachable console."""
    # Paths (drive files, the socket path) are user data, not options: leave them out of the keyword scan.
    joined = " ".join(x for x in argv if not x.startswith(("file=", "/")) and "path=" not in x)
    for bad in _FORBIDDEN:
        if bad in joined:
            raise VmError("vm_not_isolated", f"forbidden option: {bad}")
    for flag, want in (("-nic", "none"), ("-monitor", "none"), ("-sandbox", None)):
        if flag not in argv:
            raise VmError("vm_not_isolated", f"{flag} missing")
        # Every occurrence must be the safe value: a later "-nic user" would override the first "-nic none".
        values = [argv[i + 1] if i + 1 < len(argv) else None for i, a in enumerate(argv) if a == flag]
        if want and any(v != want for v in values):
            raise VmError("vm_not_isolated", f"{flag} must be {want}")
    drives = [argv[i + 1] for i, a in enumerate(argv) if a == "-drive"]
    if len(drives) != 3 or "readonly=on" not in drives[1] or "readonly=on" in drives[2]:
        raise VmError("vm_not_isolated", "input must be read-only and output read-write")
    for i, a in enumerate(argv):
        if a == "-device" and argv[i + 1].startswith("vfio-pci") and not argv[i + 1].startswith("vfio-pci,host="):
            raise VmError("vm_not_isolated", "vfio device must name its PCI address")


# --------------------------------------------------------------------------- disks (no mounts)
def _run(runner: Callable[..., Any], argv: list[str]) -> None:
    r = runner(argv, check=False, capture_output=True, timeout=600)
    if getattr(r, "returncode", 1) != 0:
        raise VmError("vm_disk_failed", argv[0])


def make_disk(runner: Callable[..., Any], path: str, size_mb: int, populate_from: Optional[str]) -> None:
    with open(path, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    _run(runner, ["mformat", "-i", path, "-F", "-v", "CCDATA", "::"])
    if populate_from and os.listdir(populate_from):
        for name in sorted(os.listdir(populate_from)):
            src = os.path.join(populate_from, name)
            if os.path.islink(src):
                raise VmError("vm_input_symlink")
            if os.path.isfile(src) and os.path.getsize(src) > FAT32_MAX_FILE:
                raise VmError("vm_input_file_too_large")
            _run(runner, ["mcopy", "-i", path, "-s", "-n", src, "::"])


def extract_output(runner: Callable[..., Any], disk: str, dest: str, max_bytes: int) -> None:
    """Copy the guest's output disk into ``dest`` with mtools, then re-check what arrived (the guest is hostile)."""
    os.makedirs(dest, exist_ok=True)
    _run(runner, ["mcopy", "-i", disk, "-s", "-n", "::", dest])
    total = files = 0
    root = os.path.realpath(dest)
    for dp_, dns, fns in os.walk(dest, followlinks=False):
        for n in fns + dns:
            p = os.path.join(dp_, n)
            if os.path.islink(p) or not os.path.realpath(p).startswith(root + os.sep) and os.path.realpath(p) != root:
                raise VmError("vm_output_unsafe")
        for n in fns:
            files += 1
            total += os.path.getsize(os.path.join(dp_, n))
            if files > MAX_OUTPUT_FILES or total > max_bytes:
                raise VmError("vm_output_too_large")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- run
def _talk(sock_path: str, payload: dict[str, Any], deadline: float, sink: list[str]) -> None:
    """Send one JSON line to the guest and collect capped progress lines. Best effort: the disk is the real output."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        for _ in range(60):
            try:
                s.connect(sock_path)
                break
            except OSError:
                time.sleep(1)
        else:
            return
        s.sendall((json.dumps(payload, sort_keys=True) + "\n").encode())
        got = 0
        buf = b""
        while time.monotonic() < deadline and got < MAX_CONTROL_BYTES:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            got += len(chunk)
            buf += chunk
            while b"\n" in buf and len(sink) < 200:
                line, buf = buf.split(b"\n", 1)
                sink.append(line[:300].decode("utf-8", "replace"))
    finally:
        s.close()


def run_vm_job(
    job: VmJob,
    *,
    work_root: str,
    out_dir: str,
    runner: Callable[..., Any] = subprocess.run,
    popen: Callable[..., Any] = subprocess.Popen,
    max_output_bytes: int = 2 * 1024**3,
) -> VmResult:
    """Run one Windows VM job. Refuses on any integrity problem; always removes the scratch files."""
    started = time.monotonic()
    if not job.image_sha256 or sha256_file(job.image_path) != job.image_sha256:
        return VmResult("failed", error_code="vm_image_hash_mismatch")
    scratch = tempfile.mkdtemp(prefix=f"ccvm-{job.job_id[:8]}-", dir=work_root)
    os.chmod(scratch, 0o700)
    proc = None
    try:
        overlay = os.path.join(scratch, "overlay.qcow2")
        _run(runner, ["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", os.path.abspath(job.image_path), overlay])
        in_img, out_img = os.path.join(scratch, "input.img"), os.path.join(scratch, "output.img")
        make_disk(runner, in_img, job.input_disk_mb, job.input_dir)
        make_disk(runner, out_img, job.output_disk_mb, None)
        sock = os.path.join(scratch, "ctl.sock")
        argv = build_qemu_command(job, overlay=overlay, input_img=in_img, output_img=out_img, control_sock=sock)
        proc = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        progress: list[str] = []
        deadline = time.monotonic() + job.timeout_seconds
        _talk(sock, {"job_id": job.job_id, **job.control_payload}, deadline, progress)
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                proc.kill()
                proc.wait(timeout=30)
                return VmResult("timeout", progress=progress, error_code="vm_timeout", duration_seconds=time.monotonic() - started)
            time.sleep(1)
        if proc.returncode != 0:
            return VmResult("failed", progress=progress, error_code="vm_exit_nonzero", duration_seconds=time.monotonic() - started)
        extract_output(runner, out_img, out_dir, max_output_bytes)
        return VmResult("success", output_dir=out_dir, progress=progress, duration_seconds=time.monotonic() - started)
    except VmError as exc:
        return VmResult("failed", error_code=exc.code, duration_seconds=time.monotonic() - started)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        shutil.rmtree(scratch, ignore_errors=True)
