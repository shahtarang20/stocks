# W14: container rental on Windows hosts (EXPERIMENTAL)

_Written 2026-11-17. Status: **built and tested with stand-ins on Linux; never run on a real Windows PC.** Builds on
[the container rental design](RENTAL-container-rental-design.md), [W13](W13-hyperv-vm-runtime-decision.md) and the managed WSL2 runtime in
`provider-agent/computecoin_agent/windows/`. Scope: **the customer's own image (and the ready-made templates) only. Kits are not touched.**_

## 1. What this is

A Windows host's agent already keeps a managed Linux system (WSL2, distro `computecoin`, user `ccrunner`) with Docker inside it. The Windows
service (the supervisor) starts the Linux agent in it with `computecoin-agent work`. This note adds one thing: the same Linux agent can also
**serve container rentals** there. Customers' containers are ordinary Linux containers (Linux images, `linux/amd64`, NVIDIA GPU only), run by the
unchanged rental code (workspace, quotas, terminal, results saving), inside a virtual machine that is separated from the owner's Windows files.

Nothing about Linux hosts changes.

## 2. The rules (and where each is enforced)

| Rule | Agent | Server |
|---|---|---|
| A Windows host keeps the `container_rental` flag only with the host's **two** switches (`allow_container_rental` and `windows_rental_enabled`), each a literal `True` | `capabilities.py` | – |
| ... and only while the platform switch `WINDOWS_RENTAL_ENABLED` is on (default **off**); `CONTAINER_RENTAL_ENABLED` and the host's opt-in are still needed | – | `capability_flags.windows_forbidden_flags()` |
| Every other Windows restriction stays: no gVisor, microVM, private tiers, vfio, multi-node, AMD | `WINDOWS_FORBIDDEN_FLAGS` | same |
| A Windows host is handed **rentals only, never a Kit session** | the runner refuses any non-rental claim | `sessions.claim_next` |
| A rental counts as a running job for the supervisor (updates and drains wait; start, drain, stop and "not online" apply) | `SessionAgent(activity=...)`, `extra_gate=control.may_claim` | – |

## 3. How it runs

1. `computecoin-agent rental on` (Windows) sets both switches in the ProgramData settings file. The host restarts the service
   (`Restart-Service ComputeCoinAgent`).
2. At start, the supervisor sets `SupervisorSettings.serve_rentals` from the two switches (`serve_rentals_enabled`). When true, the runner is
   launched as `... work --control-socket ... --serve-rentals`.
3. `work --serve-rentals` starts the rentals-only session service next to the job loop (`cli._serve_rentals_from_runner`). Inside the Linux
   system the platform is Linux, so the existing session agent runs unchanged: terminal on a real pty, workspace folder on the Linux disk,
   quotas, autosave, miner watch.
4. The heartbeat carries the `container_rental` flag, so the server lists the host and the customer's rental can be claimed.

## 4. The part that matters most: the Windows drive

WSL mounts the Windows `C:` drive inside the Linux system by default (`/mnt/c`) and lets Linux programs start Windows programs (interop). A
customer who broke out of a container would then reach the owner's files. So `rental check` on Windows **fails** when `/mnt/c` has content
and **warns** when interop is on, and tells the host how to switch both off (`/etc/wsl.conf`: `[automount] enabled=false`,
`[interop] enabled=false`, then `wsl --shutdown`). The managed distro's own provisioning should set both; this check is the proof.

## 5. What is not done, and what is not known

* **Never run on Windows.** Every `wsl.exe` flag in the existing runtime module is still marked `TODO(verify-docs)`. This work follows it.
* **The Windows agent has no published installer yet** (section 9 says what now exists for it). The repository has an MSI package source (`provider-agent/windows/installer/`),
  a provisioning script (`provider-agent/windows/provision.ps1`), build and signing scripts, a governance engine and an updater, all marked
  "written, never built or run on Windows" (see [windows-installer.md](../operations/windows-installer.md)). What is missing is building and
  signing the MSI (the signing certificate is an owner item), hosting the download, and the website side. Rental on Windows is only for
  hosts who already run the Windows agent.
* **How the Linux runner gets the machine credential** on Windows (the secret lives in the Windows secret store) is the existing runtime's
  concern; rentals use the same path as jobs and add no new one.
* **Sleep and restarts** cut a rental off on a PC. Customers are warned in the marketplace and on the Rent page.
* **Rentals count as "one job at a time"** for the supervisor, even a rental with 0 graphics cards (conservative).
* Native Windows containers are not supported (Linux images only).
* Port forwarding, disk growth of the WSL virtual disk, and the memory cap WSL applies by default (about half of the PC's memory) need real-PC
  observation.

## 6. Testing

* Stand-in tests run anywhere (`tests/test_windows_rental_runner.py`, `tests/test_windows_rental_check.py`, server tests in
  `apps/api/tests/test_container_rental.py`).
* On a real PC: `scripts/windows/rental-test-kit.ps1` (read only, writes a report), then the steps in
  [windows-rental-test-checklist.md](../operations/windows-rental-test-checklist.md).

## 7. Protections compared with Linux

The runner inside the Linux system is the same code on both, so most protections are identical. This table says where Windows relies on something
different, and where there is a gap.

| Edge case | Linux host | Windows host (rental) |
|---|---|---|
| GPU too hot, or its sensor unreadable | The runner's thermal guard stops new claims and ends a running rental; a separate heat watchdog process removes customer containers if the card is too hot and the agent is gone | The same runner check, fed the temperature by the Windows supervisor (native NVML). There is **no separate watchdog process**, but the supervisor now acts on its own as well: at the hard-stop temperature it interrupts the running job or rental (politely, then it kills the runner and removes every customer container, rentals included) |
| The owner needs the computer | Optional owner governance (off by default) | The supervisor's governance engine, then an `interrupt` that stops rentals like jobs |
| Workspace over its limit, or disk low | Checked every 30 seconds | The same check inside the Linux system, which sees only its virtual disk. **New:** the supervisor also watches the free space of the Windows drive that holds it: under 20 GB (`host_disk_min_free_gb`) no new work is taken, under 5 GB (`host_disk_stop_free_gb`) a running one is interrupted |
| Crypto miner in the container | Process-name watch | Same |
| The agent crashes or restarts mid-rental | Session containers left behind are removed when the session service starts (new) | Removed by the supervisor when it kills the runner (new) and again when the runner starts |
| The PC sleeps, hibernates or shuts down | Not handled (no sleep inhibitor); customers and hosts are warned | The supervisor's sleep inhibitor (`SetThreadExecutionState`, system awake, display may still turn off) is on while a job runs, and a rental counts as a running job. Never run for real. A shutdown or hibernate by the owner still ends the rental, and WSL stops with it |
| Power cap | Applied when the agent can change it | `nvidia-smi -pl` from the service, needs administrator |
| An agent update while a rental runs | The updater waits | The supervisor's updater waits (a rental counts as a running job) |
| A Kit session reaching the host | Allowed | **Refused** (server and runner) |

Windows already has two protections Linux lacks: the sleep inhibitor above, and the `wsl.conf` hardening its provisioning writes (automount off,
interop off, Windows path not appended; the check in section 4 proves it).

Found while writing this table: before this change, an agent restart left a running rental container behind on **Linux and Windows**, and the
Windows supervisor did not remove rental containers when it killed the runner. Both are fixed and tested.

## 8. What the official documentation says (read 2026-11-17) and what changed because of it

Pages read: Microsoft Learn "Basic commands for WSL" (2025-12-01), "Advanced settings configuration in WSL" (2026-10-16), "SetThreadExecutionState"
(2025-07-01), and NVIDIA's "CUDA on WSL User Guide" with Microsoft's GPU compute tutorial.

| Documented fact | Changed in the code |
|---|---|
| The `[boot]` section (systemd) "is only available on Windows 11 and Server 2022"; this runtime needs systemd as PID 1 | The Windows requirement is now **Windows 11** (build 22000), not Windows 10 21H2 (the host guide, checklist, kit and website text agree) |
| In WSL 2, Docker supports **only `--gpus all`** (a card cannot be picked by number), so a container gets every card the PC has | `build_gpu_argv` returns `--gpus all` inside WSL (`wsl_env.running_in_wsl`); the Windows check uses it; the server gives a Windows host a rental only for **none of its cards or all of them** (a PC with two cards is rented as two, one customer at a time) and refuses any other count up front (`windows_host_rents_all_cards`); claims skip a rental that asks for part of the cards |
| GPU compute in WSL works on GeForce and RTX cards in WDDM mode, **not** cards in TCC mode or data-centre cards | The host guide, the Windows host guide and the test kit (`NVIDIA card mode`) say so |
| Install only the Windows NVIDIA driver, never a Linux one inside WSL | Already how the runtime works; unchanged |
| `/usr/lib/wsl/lib/nvidia-smi` is the documented full path; being on `PATH` is not stated | The check tries the name, then the full path (done earlier) |
| `nvidia-smi` has "a limited feature set" in WSL (no utilisation or process queries, no mode changes) | The checks only read the name, memory and driver version; heat is read on the Windows side by the supervisor |
| `SetThreadExecutionState` is thread-scoped; it cannot stop a user-initiated sleep (lid, power button) | The sleep inhibitor already uses one long-lived thread; its docstring now records this |
| `wsl --import/--list --verbose/--version/--terminate/--unregister/--shutdown/--update/--set-version` and `--distribution`/`--user` match | Recorded in the `wsl.py` header |
| `wsl --exec`, `--manage ... --resize/--set-sparse`, `WSL_UTF8`, the UTF-16 output, and the column layout are **not** on those pages | Still unverified, listed in the `wsl.py` header; the test kit exercises them on a real PC |
| `.wslconfig`: `memory` defaults to 50% of Windows memory | Noted: a Windows host's rentals get at most what WSL gets; the agent already caps by the memory the Linux system reports |

Not read yet (the rental path does not use them): WiX v4 and `signtool` for the MSI, `msiexec` switches, Authenticode checks, `winget`.

## 9. The install path, and how to try ONE Windows PC first

Built (all unsigned, none run on a real PC):

| Piece | Where | What it does |
|---|---|---|
| Build workflow | `.github/workflows/windows-installer.yml` (a real Windows runner) | Builds the payload (a pinned portable Python plus the agent and its Windows dependencies) and the unsigned MSI with WiX. Checks that the portable Python imports the agent and pywin32, that the requirements checker runs under it and says "not met" (exit 3) on a runner with no WSL2 GPU, that the MSI holds the payload, and that the MSI **refuses** to install without the consent flag and on an unsupported computer, leaving nothing behind. Uploads the MSI for a week. Never publishes. |
| Package | `provider-agent/windows/installer/Package.wxs` | The launch condition now requires Windows 11 (`WindowsBuild >= 22000`), matching the code. |
| Build script | `.../build.ps1` | The "looks like a secret" check no longer trips on the public certificate bundle (`certifi\cacert.pem`) that the agent's own dependencies ship. |
| Website | host setup page, step 3c | A choice between Linux and **Windows 11 (experimental)**. The Windows panel shows what the PC needs and what the installer does, asks the host to confirm they read it, and only then shows ONE PowerShell block (download, checksum check, silent install with the consent flag, connect with the token typed hidden, optional `rental check` and `rental on`, start the service). **It shows nothing to copy until `NEXT_PUBLIC_WINDOWS_AGENT_MSI_URL` and `NEXT_PUBLIC_WINDOWS_AGENT_MSI_SHA256` are set.** |
| Pilot list | server setting `WINDOWS_RENTAL_MACHINE_IDS` | Lets named machine ids offer Windows rentals while `WINDOWS_RENTAL_ENABLED` stays off, so one real PC can be tried without opening it to every Windows host. |

Order to try one PC: run the workflow (manual run) and read its result; fix what it reports; get a signing certificate (or accept the "unknown publisher"
warning for a pilot); publish the MSI and set the two website variables; install on the PC from the setup page; run `rental-test-kit.ps1` and the
checklist; put that machine's id in `WINDOWS_RENTAL_MACHINE_IDS`; then, and only if everything passed, consider `WINDOWS_RENTAL_ENABLED`.

Still not possible without a real Windows 11 PC with an NVIDIA card: provisioning the managed Linux system (`provision.ps1`), WSL2 with the card, and a rental.

