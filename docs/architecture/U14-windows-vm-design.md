# U14 design note: Windows VM tier (optional)

Status: draft. **The go / no-go is the owner's.** This slice is shipped DEFAULT OFF (`WINDOWS_VM_ENABLED=false`): with
the flag off, a spec with `mode: "vm"` is refused at submit (`windows_vm_disabled`), a queued one fails validation, and
no machine is ever offered one. Turning it on is a configuration change, not a code change. No migration, no ledger,
pricing or billing change: a VM job is an ordinary v2 job billed per GPU-second like any other (P5).

## What the customer submits
`POST /v1/jobs/v2` with `mode: "vm"`, `os: "windows"` and a `vm` block (`image: dataset:<id>`, `image_sha256`, `vcpus`,
`memory_gb`, `gpu_passthrough`, `licence_ack: true`). `os` and `vm` are refused unless `mode` is `vm`; `mode: vm` is
refused without them; a VM job cannot be fanned out. The Kit still carries the signed manifest (params, outputs).
**The customer supplies a licensed Windows image and is responsible for the licence. The platform ships no Windows image
and no licence**, and `licence_ack` records the customer's confirmation. Private tiers are not offered (the Kit route
already refuses them); a Windows VM gets the container-tier trust model, not Proof-of-Privacy.

## Scheduling
`scheduler_v2.windows_vm_reason`: flag off -> `windows_vm_disabled`; GPU passthrough asked and the host has not reported
`vfio_gpu: true` -> `vfio_gpu_required`. A host reports it only when `vm_runner.advertise()` finds qemu, qemu-img,
mtools, `/dev/kvm`, `/dev/vfio/vfio`, IOMMU groups and a display device bound to `vfio-pci`. The report is
self-declared; it only decides where the customer's own VM may run.

## Isolation (agent module `provider-agent/computecoin_agent/vm_runner.py`)
QEMU/KVM microVM per job, started by the agent:
- no network device at all (`-nic none`), no monitor, no VNC/SPICE, no shared folders, no USB;
- the control channel is a virtio-serial port backed by a unix socket in a 0700 directory (host-only, capped at 64 KiB);
- the image sits behind a copy-on-write overlay (the staged image is never modified); the sha256 is re-checked at start;
- input goes in on a read-only FAT32 disk image, output comes back on a read-write one; both are built and read with
  mtools, with no mount, and the extracted output is re-checked (no symlinks, no escape, file-count and size caps);
- QEMU's seccomp sandbox is on; `assert_isolated` re-checks the argv so a later edit cannot add a network or share;
- scratch files are deleted at the end, also on failure.
"Host cannot reach the VM except through the agent" is true for host processes and the network. The host OPERATOR is
root and can always attach to their own machine; this tier gives no protection against them (that is what ADR-0005 is for).

## Unverified (needs a lab run before go)
Never run against KVM, VFIO or a real Windows image: GPU reset between jobs (VFIO function-level reset support varies
by card), guest-side job runner (a TEMPLATE is in `provider-agent/vm/guest/run-job.ps1`, never run in a Windows guest; the customer
bakes it into their image), Windows licensing terms for hosting on third-party hardware (legal review), FAT32 4 GiB
per-file limit.

Wiring (written, not run): the claim's `v2` block gains `mode: "vm"` and a `vm` block (`image` {name `vm-image`, file_id,
sha256, size_bytes}, vcpus, memory_gb, gpu_passthrough, params) for a VM job; the agent (`runtime_v2.run_vm_v2`, called from
`jobrunner.run_one`) runs it only with agent config `windows_vm_enabled: true` and, for passthrough, a host whose
`vm_runner.advertise()` says `vfio_gpu`; otherwise the job is reported failed (never run as a normal Kit). The heartbeat
health block carries `vfio_gpu: true` only when `windows_vm_enabled` is on and the host qualifies; the server sanitizes it to a
real boolean (`HeartbeatHealth`) and the scheduler also reads it from `last_health`. The image is downloaded with
`download-url` (kind `input`, name `vm-image`), hash-checked, and the guest gets `job.json` (+ declared inputs) on the input disk.
U14.1 and U14.2 need the run record and the security review.

## VM command field
The `vm` block takes an optional `command` (list of 1 to 32 non-empty strings, each <= 512 characters). It is carried unchanged into the claim's `vm` block and `runtime_v2.run_vm_v2` writes it to `job.json` for the guest runner, which runs it inside the customer's own VM. `params.vm_command` is DEPRECATED: it is still read as a fallback for one version when `vm.command` is absent, then it will be removed; new specs must use `vm.command`.

