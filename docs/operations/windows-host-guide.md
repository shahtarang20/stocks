# Sharing a Windows PC's GPU: host guide

_Cross-platform plan W14 · Written 2026-11-06 · **Status: the Windows host is not released.** The pieces exist in the repository as code that has **never been run on a Windows machine**
(supervisor, runtime provisioning, requirements checker, resource governance, updater); the installer is written (WiX source and install/uninstall actions in `provider-agent/windows/installer/`) but has never been built, signed or run. This guide describes how it is meant to work so that
the owner, testers and support can check the product against it. Do not tell anyone they can install it today. Vendor statements are `TODO(verify-docs)` unless marked **[read 2026-11-06]**._

Plain-language version for hosts: <https://computecoin.example/share/windows> (page source: `apps/web/src/app/(marketing)/share/windows/page.tsx`).
Developers who want to **work on** the code from Windows: [windows-developer-setup.md](windows-developer-setup.md).

## 1. What you are signing up for

You let other people run jobs on your PC's NVIDIA graphics card, and you earn credits when jobs finish. The agent never runs a customer's job directly on Windows. It runs inside a
**separate Linux environment** (a WSL2 distro named `computecoin`) and inside a locked container within it. Customer jobs cannot see your Windows files, your Windows programs or your home network.

**Limits, stated plainly:**

| Limit | Why |
|---|---|
| **Container tier only.** Windows hosts get ordinary jobs, never private (Sealed, Confidential), bring-your-own-container, gVisor, microVM or multi-node jobs. | A Windows administrator (you) can read a WSL2 virtual machine's memory, so no privacy promise can be made (X5). See [W12](../architecture/W12-windows-privacy-tiers-decision.md), [W13](../architecture/W13-hyperv-vm-runtime-decision.md). |
| **NVIDIA graphics cards only.** | AMD on Windows is a recorded no-go for now ([W7](../architecture/W7-amd-on-windows-decision.md)). Intel and Apple graphics are not supported. |
| **A PC you also use.** The agent steps aside while you use the GPU. | Your PC comes first (X13): see section 6. |
| **Earnings are test credits** during the preview, and nothing is promised. | Same as Linux hosts. |
| **Private jobs, AMD, multi-node, Windows-only software** are not available on Windows hosts. | See above. |

## 2. Requirements

The installer runs a **requirements checker** first and shows a plain pass or fail list (the same checks run as JSON for the website). Thresholds below come from
`computecoin_agent/windows/runtime/requirements_check.py` and are **placeholders until the owner decides** (plan section 9: "minimum Windows version and NVIDIA driver version").

| Requirement | Placeholder value | Notes |
|---|---|---|
| Processor | 64-bit Intel or AMD (x86-64) | ARM Windows is not supported yet |
| Windows | Windows 10 or 11 that supports WSL 2 with GPU compute. WSL itself needs Windows 10 version 2004 (build 19041) or later, or Windows 11 **[read 2026-11-06]**. The checker's placeholder minimum is build 19044 (Windows 10 21H2): `TODO(verify-docs)` the real minimum for GPU compute in WSL2 | Home and Pro: both must be tested (plan section 10) |
| Virtualization | Enabled in the firmware (BIOS/UEFI) | `TODO(verify-docs)` how to tell on each vendor's firmware |
| WSL | Current WSL from the Microsoft Store or `wsl --update`; the signed runtime manifest states the minimum version (placeholder 2.0.0) | |
| NVIDIA graphics card and driver | GeForce RTX 20 series or newer, or a data-centre card; at least 8 GB of video memory; a current **Windows** NVIDIA driver (placeholder 535.0). **Do not install a Linux NVIDIA driver inside the distro**: the Windows driver provides the GPU to WSL | `TODO(verify-docs)` NVIDIA's CUDA-on-WSL guide: supported cards and driver |
| Memory | 8 GB minimum, 16 GB recommended | The runtime uses part of it while it runs |
| Free disk | 60 GB on the drive that will hold the distro | The distro's virtual disk is capped |
| Internet | Outgoing connections only. **You do not open any port** | |

WSL's own settings are per user and apply to all WSL 2 distros on the account, including the VM's memory and processor limits (`%UserProfile%\.wslconfig`) **[read 2026-11-06]**. The agent sets caps for its own distro;
if you also use WSL for other things, check that file and the Windows user that owns the distro.

## 3. Install (planned flow)

1. **Check.** Run the requirements checker (from the installer, or on the website page above). Fix every red line it names. Nothing is installed by the checker.
2. **Install.** A signed installer (`TODO`: MSI plus `winget`, the owner has not chosen; the Authenticode certificate is also an owner item). It shows what will be installed, what you share (GPU time, some memory and disk, internet), and
   how to stop and remove everything, and it asks for your consent first (X14). It installs:
   * the **ComputeCoin Windows Service** (the supervisor), outbound-only firewall rules, and the data folder under `%ProgramData%\ComputeCoin`;
   * the managed **WSL2 distro `computecoin`**, imported from a pinned image whose hash is checked (a mismatch stops the install), hardened so it cannot reach your Windows files (drive mounts off, Windows program interop off, Windows PATH off), with Docker Engine and the NVIDIA container toolkit installed from pinned versions.
     The settings that do this are the documented `wsl.conf` options `[automount] enabled=false`, `[interop] enabled=false`, `appendWindowsPath=false` and `[boot] systemd=true` **[read 2026-11-06]**. One caveat from the same page: the `[boot]` section is documented as available on Windows 11 and Server 2022 only, `TODO(verify-docs)` what that means for Windows 10;
   * a **GPU smoke test**: a tiny CUDA container must see your card before enrolment is allowed to finish.
3. If the smoke test fails, nothing is enrolled. See troubleshooting.

## 4. Enrol

Create a setup token on the website (**Provider, Add my computer**). It is a one-time code that lasts 1 hour. Then, in an **administrator** PowerShell:

```powershell
computecoin-agent service enroll --server https://api.computecoin.example --token <your-token>
computecoin-agent service install
computecoin-agent service start
computecoin-agent service status
```

The credential the server returns is stored with Windows DPAPI under `%ProgramData%\ComputeCoin\secrets`; no plain-text copy is kept (X7). The folder is restricted to the system and local administrators. Do not paste
the token anywhere public: it works once, but treat it like a password.

If you previously ran the agent in a developer setup with `agent.toml`, `computecoin-agent service migrate` moves the old plain file into DPAPI. Check the commands with `computecoin-agent service --help`: they come from `windows/cli_commands.py`, which has never been run on Windows.

## 5. What runs on your PC

| Piece | Where | What it does |
|---|---|---|
| **ComputeCoin service** (supervisor) | Windows Service, starts with Windows | Talks to ComputeCoin (outgoing only), starts and watches the distro, reads GPU temperature and power natively, decides when to claim work |
| **Runtime distro `computecoin`** | WSL2, no access to your Windows drives | Runs the same Linux agent as Linux hosts, Docker, and the job containers |
| **Job containers** | Inside the distro | One customer job each: no network by default, read-only, no extra privileges, no access to your files |
| **Logs** | `%ProgramData%\ComputeCoin\logs` | Rotating log files; secrets are never written to them |

Other things the agent does for you (all configurable, conservative defaults): hardware **temperature caps** (stop claiming at the cap, stop the job at the hard stop), a **video-memory reserve** for you (placeholder: 1 GB), **no new work on battery**, **no new work during Windows Update restarts**,
**stay awake only while a job runs**. Details: `docs/architecture/windows-control-channel.md` and `computecoin_agent/windows/governance/policy.py`.

## 6. When you use your PC

By default the agent **yields to you**: it does not take new jobs while a program is using the GPU, while you are active at the keyboard or mouse, or while a full-screen app is open. A running job is finished, not killed,
unless you press "stop now". Hours can be limited with a schedule. The console shows the machine as **yielding** (it counts as available later, not as broken). What counts as "using the GPU" and the defaults are owner decisions
that are still open (plan section 9): the values in `agent.toml` (`governance_*`) are the starting point.

## 7. Stop and remove

| I want to | Command or action |
|---|---|
| Pause for now | Website: set the machine to maintenance. Or `computecoin-agent service stop` (administrator). A running job is finished or failed over without charging the customer. |
| Stop sharing completely, keep the software | `computecoin-agent service stop`, then `computecoin-agent service remove` removes the service registration. |
| Remove everything | Uninstall from **Settings, Apps** (the installer's uninstaller): it removes the service, the `computecoin` distro (with its virtual disk), the firewall rules and the stored credential. The removal is written (`cleanup` in `provider-agent/windows/installer/msi_actions.py`, wired into `Package.wxs`; X14, W6.3 "no residue") but has never been run on a Windows machine, so the check below is how to prove it. |
| Check nothing is left | `wsl --list --verbose` must not list `computecoin`; `Get-Service` must not list the ComputeCoin service; the folder `%ProgramData%\ComputeCoin` should be gone. |

## 8. Troubleshooting

First collect a diagnostics file; it is safe to send (secrets are replaced, and nothing is written if anything still looks like a secret):

```powershell
computecoin-agent support-bundle --out "$env:USERPROFILE\Desktop\computecoin-support.zip"
```

Open the zip and read it before you send it.

| Symptom | What to try |
|---|---|
| The requirements checker says **virtualization is off** | Turn on the CPU virtualization option in your firmware setup (named differently per maker), then run the checker again. |
| It says **WSL is missing or old** | Open PowerShell as administrator: `wsl --install` for a new install (restart afterwards) or `wsl --update` **[read 2026-11-06]**: `wsl --install` is documented for Windows 10 2004+ and Windows 11. |
| It says **the NVIDIA driver is missing or too old** | Install the current driver for your card from NVIDIA for **Windows**. Do not install a Linux driver. Run the checker again. |
| **The GPU smoke test failed** | Reboot once; run `wsl --shutdown` then retry; check `nvidia-smi` works in a normal Windows prompt; update the driver. If it still fails send the support bundle. `TODO(verify-docs)` the expected output of `nvidia-smi` inside the distro. |
| **The machine shows as "yielding" all the time** | You are using the GPU, or a background program is. Check the schedule, and whether something keeps the GPU busy (a game launcher, video editor, browser with hardware acceleration). |
| **The machine shows "maintenance"** | Windows has a pending restart, or you paused it. Restart Windows; resume on the website. |
| **No jobs arrive** | Real demand may be low; the machine may be yielding; the price may be too high; private and BYOC jobs never go to Windows hosts. Look at the host console for the reason (the fleet view shows the missing capability). |
| **Your PC feels slow while a job runs** | Lower the caps (memory, processors) in `%UserProfile%\.wslconfig` for the owner account **[read 2026-11-06]**: the file applies to all WSL 2 distros of that user, restart with `wsl --shutdown`. Raise the video-memory reserve. |
| **The disk fills up** | The distro's virtual disk is capped (placeholder 60 GB free required at install). Reclaim with the repair command once it exists (`TODO`, W3.5). |
| **Antivirus warns about the service or the distro** | Do not disable your antivirus. Send the support bundle and the warning text; the installer is meant to be code-signed (not yet), and the agent does no mining. |
| **After Windows Update the distro is gone or unhealthy** | Run `computecoin-agent service status`; send the support bundle. The runtime has a repair path in code (`RuntimeProvisioner.repair`) that has never run on a real machine. |

## 9. What is not proven yet

* Nothing in sections 3 to 8 has been run on a Windows machine. Hardware items are open: GPU smoke test, isolation suite against the real runtime (read `C:\`, reach Windows interop, reach the LAN), parity with a Linux host, the 24-hour soak (see `.github/workflows/windows-host-e2e.yml` and `tests/windows/`).
* Version thresholds, the installer technology, the signing certificate, the yield defaults and the payout policy are owner decisions that are still open.
* The website page and this guide must be updated the moment any of that changes.
