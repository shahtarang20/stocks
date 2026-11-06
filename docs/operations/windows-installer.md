# Windows installer, updater and resource governance (W5, W6)

_Status date: 2026-11-06 · Owner: engineering · Status: **written, never built or run on Windows**._
Plan: [cross-platform-windows-linux-master-plan.md](cross-platform-windows-linux-master-plan.md) (W5, W6, X10, X13, X14).
Threat model: [../architecture/W11-windows-threat-model.md](../architecture/W11-windows-threat-model.md).

Every Windows, WiX, signtool, winget and msiexec detail is marked `TODO(verify-docs)` in the source (X11). Nothing here has
been checked against current vendor documentation, and none of it has been executed. Tick no checklist item from this page
without a recorded run on a clean Windows VM.

## 1. What exists

| Piece | Where | Purpose |
|---|---|---|
| Governance policy engine | `provider-agent/computecoin_agent/windows/governance/policy.py` | Signals in, `online / yielding / maintenance / draining` out, with hysteresis. Pure Python, works on Linux desktops too (X13). |
| Windows probes | `.../governance/probes.py` | `GetLastInputInfo`, `SHQueryUserNotificationState`, foreground window and full-screen, NVML process list, reboot-pending registry keys, `GetSystemPowerStatus`, `SetThreadExecutionState`. |
| Session snapshot | `.../governance/snapshot.py` | The user-session helper's report format and the probe the service reads (see section 3). |
| Guard glue | `.../governance/integration.py` | `GovernedGuard` plugs into the existing `thermal_guard` slot of `poll_loop` and `heartbeat_loop`. |
| Updater | `provider-agent/computecoin_agent/updater/` | Signed manifest, Ed25519 verify, rings, download, hash, install, health check, rollback, claim gate. |
| Release API | `apps/api/app/api/routers/agent_releases.py`, `.../services/agent_releases.py`, migration `0042_agent_releases` | Publish, pause, resume, roll back, assign rings; host manifest route. |
| MSI source | `provider-agent/windows/installer/Package.wxs` | WiX v4 package. |
| MSI actions | `provider-agent/windows/installer/msi_actions.py` | Service registration, firewall rule, full cleanup. Standard library only, testable on Linux. |
| Build and sign | `provider-agent/windows/installer/build.ps1` | consent text to RTF, `wix build`, `signtool sign` and `verify`, SHA-256. |
| Offline manifest signing | `provider-agent/windows/installer/sign_manifest.py` | Hash the signed MSI, sign the update manifest with the update key. |
| winget templates | `provider-agent/windows/installer/winget/` | Version, installer and locale manifests (placeholders only). |
| Consent text | `provider-agent/windows/installer/consent.txt` | The consent screen (X14). |

## 2. Owner decisions this work waits on

| Decision | Why it matters | Default used in the sources |
|---|---|---|
| Code-signing certificate and who holds it | `build.ps1` refuses to sign without one; the certificate is never in the repository | Thumbprint or PFX path from the environment |
| **Update signing key** (Ed25519) and its custody | No manifest is accepted until its public key is pinned in `updater/manifest.py` `PINNED_PUBLIC_KEYS` and set as `AGENT_RELEASE_SIGNING_PUBLIC_KEY` on the API | Empty: every manifest is refused (fail closed) |
| Payload shape | The MSI assumes a relocatable Python at `<INSTALLFOLDER>\python\` with `computecoin-agent` installed (embeddable distribution). A frozen executable would change the custom actions | Bundled Python, called by module path |
| Service account | A WSL2 distro belongs to one Windows user, so a service running as LocalSystem may not see the distro the installing user created. Open question for W2/W3 | LocalSystem (the supervisor package's default) |
| Artifact host and allow-list | `AgentUpdater(allowed_hosts=...)` can pin the download host | https only, no host list |
| Windows Update reboot policy, default schedule, VRAM reserve | Governance defaults (plan section 9) | yield on, idle 300 s, no battery, 1024 MB reserve |
| Forced-update floor policy | Only for security fixes (plan risk table) | Floor set per release in the signed manifest |

## 3. Resource governance (W5)

### 3.1 Decision table

| State | Meaning | New claims | Running job |
|---|---|---|---|
| `online` | Quiet and allowed | yes | runs |
| `yielding` | The owner has the machine: foreground GPU app, recent input, full-screen app, battery, outside the schedule, VRAM reserve, `stop now`, or a probe that failed | no | none |
| `maintenance` | The OS needs the machine: Windows Update reboot pending, sleep or hibernate | no | none |
| `draining` | One of the above while a job is still running | no | finishes; stopped only by `stop now` (or full-screen when `governance_preempt_on_fullscreen` is on) |

Defaults (X13, all conservative): yield to the owner on; **never** during a full-screen app (cannot be turned off); not on
battery; unknown signals block; idle threshold 300 s; 60 s of unbroken quiet before going back online (hysteresis);
a clock that jumps backwards restarts that count; VRAM reserve 1024 MB (only checked when no job of ours runs, so the host never
yields to its own job); sleep is prevented only while a job runs and never the display.

A server or datacenter host with no desktop sets `governance_headless = true`: activity signals no longer need to be known,
but full-screen, battery, reboot, sleep, schedule, VRAM and `stop now` still apply.

### 3.2 Config fields (additive, in `AgentConfig`)

`governance_enabled` (None = on for Windows, off elsewhere), `governance_headless`, `governance_yield_to_owner`,
`governance_idle_threshold_seconds`, `governance_resume_clear_seconds`, `governance_allow_on_battery`,
`governance_vram_reserve_mb`, `governance_schedule` (list of `{days, start, end, timezone}`; a typo or unknown time zone is
refused at load), `governance_ignored_processes`, `governance_stop_now_hold_minutes`, `governance_preempt_on_fullscreen`.
On Windows the `tzdata` Python package must be installed or no time zone resolves (the window then never matches, and a
config naming it is refused).

### 3.3 Things the integrator must know

1. **Session 0.** `GetLastInputInfo`, the foreground window and the full-screen query describe the interactive session; a Windows
   Service lives in session 0 and sees none of it. Run `python -m computecoin_agent.windows.governance.probes --snapshot-json`
   in the user's session (a logon task or a per-session helper) and feed its lines to `snapshot.SnapshotProbe`. A stale report
   blocks claims; "no user logged on" (`no_session`) counts as owner away.
2. **Heartbeat values.** The API accepts `online | maintenance | offline | throttled` and treats `maintenance` as a sticky owner hold
   that a later `online` heartbeat does not undo (`agents.py`, owner_hold). So every non-online governance state is reported as
   `throttled` and the real state goes in the additive health field `availability_detail` (`Decision.heartbeat_health()`).
   The server does not store that field yet (W1/W10 follow-up).
3. **Suspend cannot be anticipated.** Windows gives roughly two seconds after `PBT_APMSUSPEND`. The real protection is the
   sleep-prevention request while a job runs plus checkpoints; the latch only reports a suspend already announced.
4. **NVML under WDDM** may list no per-process GPU memory, so `foreground_gpu_app` can stay empty; full-screen and recent input
   then carry the protection.
5. **Wiring.** Nothing edits `cli.py` or `jobrunner.py`. The supervisor builds `GovernedGuard(engine, collect, thermal=ThermalGuard(...))`
   and passes it where `thermal_guard` goes, calls `SleepInhibitor.sync(decision.prevent_sleep)` from `on_decision`, and drives
   `JobTracker.begin/end` from the control channel's `job_started` / `job_finished`.
6. Disk and memory caps are the runtime distro's job (`windows/runtime`, `.wslconfig` limits), not this engine's.

## 4. Updates (W6)

### 4.1 Manifest

One flat JSON object, signed with Ed25519 over `b"computecoin-agent-update-v1\n" + canonical_json(object without "signature")`
(sorted keys, compact separators). Fields: `schema`, `product`, `version`, `platform`, `artifact_url` (https), `sha256`,
`size_bytes`, `min_supported`, `ring`, `issued_at`, optional `expires_at`, optional `revoked_versions`, and `signature`.
Strict: unknown fields are refused. The signature is checked before any field is read.

### 4.2 Rings

`canary` < `early` < `general`. A release carries the **widest** ring it is offered to; a host in ring R gets releases whose ring is R or
wider. The **server** assigns each machine a ring (`PUT /v1/admin/agent-releases/rings/{machine_id}`; none = `general`). The host
reports its ring (`update_ring`), which can only move it to a **wider** ring (fewer releases), never narrower. The agent also refuses a
manifest for a narrower ring than its own. Promotion canary to early to general is a new signed manifest for the same version with
a wider ring.

### 4.3 Flow (`AgentUpdater.run_once`)

fetch, verify signature and fields, record the floor, decide, **if a job runs ask the gate to drain and come back later**, download to
a fresh temp directory, check size and SHA-256, copy to the cache and hash again, persist `in_flight`, install through the injected
installer, wait for N consecutive healthy checks **that also see the new version installed**, otherwise reinstall the previous known-good
artifact (hash-checked first) and confirm it came back healthy. If it cannot be restored the host is blocked from claiming and the result
is `rollback_failed` (alert). A failed version is not retried for 6 hours. A downgrade is refused unless the signed manifest lists the
running version in `revoked_versions`.

**Known gap:** the first update after a fresh install has no cached previous artifact, so a failed health check cannot be rolled back
automatically (the host is blocked and alerts). Seeding `known_good` at install time needs a copy of the installed MSI in the data
directory; decide how (bundle, or the first successful health-checked run records it).

### 4.4 Forced-update floor

`min_supported` in a **verified** manifest is stored and survives restarts; a host below it hands out no job slot until it has updated.
It still drains first: never during a job.

### 4.5 Release procedure (checklist)

1. Build and sign the MSI: `build.ps1 -Version x.y.z -PayloadDir <folder outside the repo>`; copy the printed SHA-256.
2. Upload the MSI to the artifact host.
3. Build and sign the manifest offline: `sign_manifest.py ... --ring canary --split`.
4. `POST /v1/admin/agent-releases` with `{manifest, signature}`; assign a few machines to `canary`.
5. Watch alerts (update rollbacks), then publish the same version with `--ring early`, later `--ring general`.
6. Pause: `POST .../{id}/pause`. Roll back: `POST .../{id}/rollback`, with a signed replacement that lists the bad version in `revoked_versions` so hosts already on it move.
7. Keep `winget/ComputeCoin.Agent.locale.en-US.yaml` Agreements text equal to `consent.txt`.

## 5. Installer behaviour

| Event | Behaviour |
|---|---|
| Interactive install | `WixUI_Minimal` shows `consent.txt` (as RTF) and needs the box ticked |
| Silent install (winget, `/qn`) | Refused unless `LicenseAccepted=1` is passed; winget passes it only after the person accepts the Agreements text |
| Install | copy files; run the requirements checker by module path (`python -m computecoin_agent.windows.runtime.requirements_check`, exit 3 aborts and rolls back); register the service (not started); add one **outbound** allow rule for the bundled Python. It does not provision the distro and does not enrol the PC |
| Upgrade | `MajorUpgrade`; service stopped first; data directory, credentials and distro kept; an enrolled host's service is started again |
| Uninstall | `msi_actions.py cleanup`: stop and remove the service, remove the runtime distro, delete the firewall rule, delete Credential Manager entries named `ComputeCoin...`, delete `%ProgramData%\ComputeCoin`. Never on upgrade; failures are listed, never block the uninstall |

Known limits: the requirements checker's output is not shown by an MSI custom action (a failure shows a generic message, so the same
check is also the pre-flight on the website and in winget docs); firewall removal and distro removal depend on the account that runs them.

## 6. Test inventory (written, not run)

`provider-agent/tests/test_w5_governance.py`, `test_w6_updater.py`, `test_w6_installer_files.py`; `apps/api/tests/test_w6_agent_releases.py`.
Each behaviour has a test that fails without it; bug re-insertion (L5) has not been done.

## 7. External interfaces to verify (X11), none checked yet

WiX v4 (`Package`, `Files`, `Launch`, `ServiceControl`, `CustomAction` type 34, `WixToolset.UI.wixext`, `InstallExecuteSequence/Custom`),
`signtool sign /fd /td /tr /sha1 /f /p` and `verify /pa`, msiexec switches and exit codes, `netsh advfirewall`, `cmdkey`, `wsl.exe --unregister`,
winget manifest schema 1.6.0, every Win32 call listed in `probes.py`, NVML process APIs under WDDM, `SERVICE_CONTROL_POWEREVENT` codes.
Record URL and date for each in [external-interfaces-checked.md](external-interfaces-checked.md) when done (S4).
