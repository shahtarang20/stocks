# Windows runtime provisioning (managed WSL2 distro)

_Cross-platform plan packages W3, W7 and W8 · Status: **code written, never run on Windows** · Every WSL, Docker, NVIDIA and
Windows interface below is an assumption marked `TODO(verify-docs)` (X11); nothing was checked against current vendor
documentation or tested on a machine._

A Windows host runs jobs in a **dedicated WSL2 distro named `computecoin`** (X2). This page describes how it is built,
checked, repaired and removed. The code is in `provider-agent/computecoin_agent/windows/runtime/`; the human entry point is
`provider-agent/windows/provision.ps1`.

| File | Purpose |
|---|---|
| `windows/runtime/manifest.py` | Signed `runtime-manifest.json`: Ed25519 verification against a **pinned public key**, strict schema |
| `windows/runtime/wsl.py` | Exact `wsl.exe` argv builders, the injectable runner, output decoding, refusal of shell programs |
| `windows/runtime/provision.py` | The idempotent provisioner: `provision`, `repair`, `reset`, `remove`, `health`, enrolment gate, CLI |
| `windows/runtime/config_files.py` | `/etc/wsl.conf`, systemd slice, `daemon.json`, non-clobbering `.wslconfig` merge |
| `windows/runtime/health.py` | Health checks and the GPU smoke test |
| `windows/runtime/gpu.py` | W7: native NVML thermal, `nvidia-smi -pl`, `--gpus device=` check, capability flags |
| `windows/runtime/network.py` | W8: egress proxy provisioning step and its verification spec |
| `windows/runtime/requirements_check.py` | Requirements checker (installer, `provision.ps1 -Action check`, website JSON) |
| `windows/provision.ps1` | PowerShell wrapper: prerequisites, clear errors, exit codes |
| `windows/wsl.conf.template`, `windows/runtime-settings.template.json` | Config templates |
| `windows/setup-multinode-network.ps1` | EXPERIMENTAL, off by default (see [W8](../architecture/W8-multinode-on-windows.md)) |
| `tests/escape/test_windows_runtime_escape.py` | Opt-in escape tests (W11.2) |

## 1. Rules the code follows

* **No shell, ever.** Every command is an argv `list[str]` run with `shell=False`. Inside the distro the program is run with
  `wsl.exe --distribution computecoin --user <u> --exec <program> <args>`. `assert_no_shell` refuses `sh`, `bash`, `cmd`,
  `powershell` and friends as the in-distro program. File content goes in through stdin (`tee`), never `echo` or redirection.
* **Fail closed (L1).** A bad signature, a digest mismatch, a repository key whose fingerprint is not the pinned one, an old
  WSL, a failed hardening check or a failed GPU smoke test stops the run or blocks enrolment. Nothing falls back.
* **Injectable.** The exec layer (`Runner`), the downloader (`Fetcher`) and the host file operations (`LocalFs`) are
  parameters, so the tests use fakes (`tests/windows_runtime/fakes.py`).
* **Credentials are never touched.** The module has no credential path. Machine credentials live in the supervisor's OS secret
  store (DPAPI, X7), outside the distro, so `repair` and `reset` cannot lose them. The supervisor's control token
  (`/etc/computecoin/control.token`) lives inside the distro: `repair` keeps it, `reset` and `remove` delete it with the distro and the
  supervisor must provision a new one (`WslRunnerLauncher.provision_token`).

## 2. The signed manifest

`runtime-manifest.json` is an envelope `{format, key_id, payload, signature}`. `payload` is the base64 of the manifest JSON;
`signature` is an Ed25519 signature over `b"computecoin-runtime-manifest/1\n" + payload`. The public key is the setting
`manifest_public_key` (32 bytes, base64 or hex) in the runtime settings file; empty, malformed or all-zero means **every
manifest is refused**. The manifest is read only from the signed bytes; a field placed next to them is ignored (a test pins
this). `sequence` must not go down (anti-rollback) unless the owner sets `allow_downgrade`.

What the manifest pins: rootfs URL, `sha256` and size; the Docker apt repository (https URI, suite, component, **signing key
URL and 40-hex fingerprint**) and the exact versions of `docker-ce`, `docker-ce-cli`, `containerd.io`; the same for the NVIDIA
container toolkit's four packages; the runner wheel and a hashed dependency lock (each with `sha256`); the egress proxy image and
the smoke-test image (both `@sha256:` digests). The apt source line is **built from structured fields**; no free-form line is
accepted, and every version string must start with a letter or digit so it cannot be read as an option.

Signing is a release step (`manifest.sign_manifest` is the helper); the private key never lives on a host or in this repository.

## 3. What `provision` does

| Step | Detail | Skipped when |
|---|---|---|
| preflight | `wsl.exe --version` must be at least the manifest's `min_wsl_version` | never (cheap) |
| import_distro | download the rootfs, **compare sha256 and size with the manifest (mismatch deletes the file and aborts before import)**, `wsl.exe --import computecoin <install_dir> <tar> --version 2`, delete the tarball | the distro is registered |
| runner_user | `useradd --system ... --shell /usr/sbin/nologin ccrunner`, password locked | the user exists |
| wsl_conf | write `/etc/wsl.conf`, `wsl.exe --terminate computecoin` (only our distro), wait for systemd, re-read and verify | fingerprint unchanged |
| limits | systemd slice `computecoin.slice` (MemoryMax, CPUQuota), virtual disk cap `wsl.exe --manage computecoin --resize <N>GB`, sparse disk | fingerprint unchanged |
| disable_ssh | `systemctl disable --now` and `mask` for `ssh.service`, `ssh.socket`, `sshd.service` | fingerprint unchanged |
| docker_engine | key download, **fingerprint must equal the manifest pin**, dearmor, deb822 source with `Signed-By`, `apt-get install pkg=version ...`, `apt-mark hold`, runner added to the `docker` group | fingerprint unchanged |
| nvidia_toolkit | same pattern for the four toolkit packages | fingerprint unchanged |
| docker_config | `daemon.json` (cgroup-parent, `no-new-privileges`, no `userland-proxy`, no TCP listener), `nvidia-ctk runtime configure --runtime=docker`, restart docker | fingerprint unchanged |
| runner_install | wheel and lock downloaded on the host, sha256 checked, copied in through stdin, **sha256 checked again inside the distro**, venv, `pip install --require-hashes --only-binary=:all: --no-deps -r lock`, then the wheel with `--no-index`; `/opt/computecoin` becomes root-owned and not group/world-writable | fingerprint unchanged |
| egress_proxy | pull the digest-pinned proxy image, write `COMPUTECOIN_EGRESS_PROXY_IMAGE=` to `/etc/computecoin/runtime.env` (non-secret) | fingerprint unchanged |
| smoke_image | pull the digest-pinned CUDA image | fingerprint unchanged |
| **verification** (never cached) | health checks, **GPU smoke test**, egress verification | never |

**Idempotency.** After each step a fingerprint (step name, its manifest inputs, the fingerprints of the steps it depends on) is
stored in `/etc/computecoin/provision-state.json` in the distro. An unchanged fingerprint skips the step; a new manifest re-runs
only the affected steps and their dependants. The state file is a convenience, not a proof: health and the smoke test always look at the
live distro. `repair` forces every step.

## 4. Hardening applied (X6) and what remains

`/etc/wsl.conf` (the same text as `windows/wsl.conf.template`):

```
[automount]   enabled=false, mountFsTab=false
[interop]     enabled=false, appendWindowsPath=false
[boot]        systemd=true
[user]        default=ccrunner
```

Also: non-root runner user with a locked password and no login shell; SSH masked; no TCP listener except loopback (checked); no
published ports; runner code root-owned.

Health checks (`health.run_health`) read the live distro: registered as WSL 2; `wsl.conf` keys; systemd is PID 1; **no
Windows-backed mount** (9p, drvfs, virtiofs) except WSL's own GPU mounts; **no `WSLInterop` binfmt entry**; no non-loopback
listener; SSH disabled; runner user unprivileged; Docker at the pinned version; NVIDIA toolkit at the pinned version with the
`nvidia` runtime registered; runner wheel at the pinned version; `/dev/dxg` and the WSL GPU library directory present.

**Residual risks, stated plainly (ADR-0003 applies):**

1. The distro runs with the **Windows permissions of the account that owns it** (WSL distros are per user). A container escape
   gives root in the distro; with automount and interop off the Windows drive is not mounted, but root can in principle try to
   mount it again. Mitigation: run the supervisor and the distro under a **dedicated low-privilege Windows account** that
   cannot read the owner's files. `TODO(verify-docs)`: whether root in a distro can re-mount drives with automount disabled.
2. Membership of the `docker` group is root-equivalent inside the distro. The distro, not the runner user, is the security
   boundary towards Windows.
3. The first start after `wsl --import` happens **before** our `wsl.conf` exists, so automount may be on for a few seconds. The
   rootfs build must ship the hardened `/etc/wsl.conf` baked in; the module then rewrites and verifies it and restarts the distro.
4. WSL2's VM memory is readable by the Windows host: no privacy tier is offered (X5).

## 5. Resource limits and `.wslconfig`

`%UserProfile%\.wslconfig` is **one file per Windows user** and configures the **single WSL2 utility VM that all of that user's
distros share, including the owner's own**. There is no per-distro memory or CPU key. So:

* **Default (`wslconfig_policy: "never"`)**: the file is never read or written.
* The per-distro caps are enforced inside the distro: `computecoin.slice` (`MemoryMax`, `MemorySwapMax=0`, `CPUQuota`) is
  Docker's `cgroup-parent`, so **all job containers together** are capped; the virtual disk is capped with
  `wsl --manage computecoin --resize`. Provisioning fails closed if the disk cap cannot be applied (`require_disk_cap`).
* **`add_missing`** (owner opt-in): adds only keys the owner has **not** set (`memory`, `processors` from `vm_memory_mb`,
  `vm_processors`), each preceded by a marker comment, with a one-time backup `.wslconfig.computecoin.bak`; `remove` deletes exactly the
  lines it added. It reports `restart_required`; the module **never runs `wsl --shutdown`** because that would stop the owner's
  other distros. The owner decides when to restart WSL.
* The supervisor's `set_limits` calls `apply_limits`, which rewrites the slice only when it changed.

## 6. The enrolment gate (W3.4)

`enrolment_gate()` runs the health checks **and** `docker run --rm --pull never --network none --read-only --cap-drop ALL
--security-opt no-new-privileges --user 65534:65534 --gpus all <cuda image> nvidia-smi -L` as the runner user. It allows
enrolment only if every health check passes, the exit code is 0 and at least `min_gpus` `GPU n: ... (UUID: ...)` lines are
printed. It is a **live** check (the state file is not consulted), and `provision` calls the same checks, so a failed smoke
test leaves `enrolment_allowed: false` and `ok: false`. The enrolment flow must call `assert_ready_for_enrolment` (raises
`RuntimeNotReady`) before it completes.

## 7. Repair, reset, remove

| Command | Effect | Data |
|---|---|---|
| `repair` | every step re-runs in place (force); distro is kept | Docker images and caches kept; control token kept |
| `reset` | manifest verified first, then unregister the distro and provision a new one | everything in the distro is lost (credentials are not in it) |
| `remove` | `wsl --unregister computecoin`, delete the install directory (refused unless it is at least 3 levels deep and names the distro), remove only the `.wslconfig` lines ComputeCoin added | as `reset`; idempotent when already absent |

## 8. W7 and W8 summary

* **Thermal.** Native NVML from the Windows side (`NvmlProbe`) feeds the unchanged `ThermalGuard`. It never returns the simulated
  fallback sample, and no reading means no block and no trigger (B6 contract).
* **Power limit.** `nvidia-smi -i N -pl W`, only from an elevated process, only inside the driver's reported min/max, best
  effort, **never fails enrolment**, not persistent (re-apply at every supervisor start). Many GeForce boards refuse it
  (`TODO(verify-docs)`).
* **Multi-GPU.** The runner's `GpuAllocator` and `nvidia_gpus_args` are reused. `verify_device_selection` is the smoke-test spec
  that `--gpus '"device=N"'` selects exactly one card (UUID equals the one `--gpus all` lists at that index), that `"device=0,1"`
  selects two and that an index past the last GPU fails. `multi_gpu` is advertised only after it passes (hardware item W7.2).
* **AMD.** No-go by default: [W7 decision](../architecture/W7-amd-on-windows-decision.md). `gpu.FORBIDDEN_ON_WINDOWS` lists every flag a
  Windows runtime must never report, including `amd_rocm`.
* **Egress proxy.** Runs inside the distro unchanged (`workloads/egress-proxy`, `DockerExecutor._setup_egress`). Provisioning pulls
  the pinned image and records it; `verify_egress_proxy` proves E1 to E4 offline (see [W8](../architecture/W8-multinode-on-windows.md)).
  A failed verification withholds the `egress_proxy` capability but does not block enrolment.
* **Multi-node.** No-go by default ([W8](../architecture/W8-multinode-on-windows.md)).

## 9. Exit codes of the Python CLI and `provision.ps1`

`0` ok · `1` failed (see `error` and `steps` in the JSON; `repair` can continue) · `2` bad arguments or settings · `3` a requirement
is not met · `4` signature or digest check failed (nothing was installed).

## 10. Running it (not run yet)

```powershell
.\provision.ps1 -Action check
.\provision.ps1 -Action provision -SettingsPath C:\ProgramData\ComputeCoin\runtime-settings.json -ManifestPath C:\ProgramData\ComputeCoin\runtime-manifest.json
.\provision.ps1 -Action gate      -SettingsPath ... -ManifestPath ...
```

Tests (written, not run): `provider-agent/tests/windows_runtime/` (fake runners) and, on a Windows host with the distro,
`COMPUTECOIN_ESCAPE_TESTS=1 COMPUTECOIN_WINDOWS_RUNTIME=1 COMPUTECOIN_ESCAPE_IMAGE=<digest-pinned python image> pytest tests/escape/test_windows_runtime_escape.py`.

## 11. Rootfs requirements (for the release pipeline)

The pinned rootfs (Ubuntu LTS, `amd64`) must contain: systemd, `apt`, `curl`, `gnupg`, `ca-certificates`, `iproute2` (`ss`, `ip`),
`python3` with the `venv` module, `coreutils`, **a hardened `/etc/wsl.conf` baked in**, and **no** SSH server, no Linux NVIDIA driver (the
driver comes from Windows), no extra users. `TODO(verify-docs)`: the exact base image and how WSL expects an imported rootfs.

## 12. Unverified assumptions (`TODO(verify-docs)` index)

`wsl.exe` flags (`--import`, `--unregister`, `--terminate`, `--list --verbose`, `--version`, `--manage --resize`, `--manage --set-sparse`,
`--distribution`, `--user`, `--exec`) and `WSL_UTF8`; `wsl.conf` and `.wslconfig` keys; per-user distro ownership under a service
account; systemd cgroup v2 controllers in the WSL2 kernel and Docker `cgroup-parent`; Docker and NVIDIA toolkit apt repository layout,
key fingerprints and package pinning; `nvidia-ctk runtime configure` flags; `/dev/dxg`, `/usr/lib/wsl/*` with automount disabled;
`--gpus device=` semantics under WSL2; `nvidia-smi -pl` on Windows; NVML on Windows; Windows minimum build, NVIDIA minimum driver
(owner decision); the requirements probes (CIM, `wsl --version`, `nvidia-smi`).
