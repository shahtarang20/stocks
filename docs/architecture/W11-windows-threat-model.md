# W11: Windows host threat model and review checklist

_Status date: 2026-11-06 · Owner: engineering · Status: **draft for independent review (W11.1, W11.3). Nothing here has been tested on Windows.**_
Plan: [cross-platform-windows-linux-master-plan.md](../operations/cross-platform-windows-linux-master-plan.md) (X1-X15, section 4.4).
Related: [ADR-0003 isolation](ADR-0003-isolation.md), [ADR-0005 privacy](ADR-0005-proof-of-privacy.md), [windows-installer.md](../operations/windows-installer.md).

Evidence labels used below: **design** (stated in the plan or written in code, not run), **tested-on-Linux** (a written unit test with fakes; the
task forbade running tests, so read this as "test exists", not "test passed"), **hardware** (needs a recorded run on real Windows, none exists).

## 1. Scope

A Windows PC that shares its GPU: the native supervisor (Windows Service), the managed WSL2 distro `computecoin` with the unchanged Linux runner and
Docker, the MSI and updater, and the control plane's release API. Out of scope here: the Linux host path (ADR-0003), privacy tiers (not offered on
Windows, X5; W12), customers' browsers.

## 2. Assets

| ID | Asset | Why it matters | Where it lives |
|---|---|---|---|
| A1 | Windows owner's files, drives, browser profiles, credentials | The owner is the victim if a job escapes | Windows side |
| A2 | Machine credential (`msk_...`, HMAC signing key) | Impersonate the host, claim jobs, earn payouts, read jobs | DPAPI blobs in `%ProgramData%\ComputeCoin\secrets` (X7) |
| A3 | Supervisor control token | Drive the runner (start, drain, set limits) | DPAPI store; the runner's copy inside the distro |
| A4 | Update signing key (Ed25519, offline) | Anyone holding it controls every host on the fleet | Release engineer, offline or HSM. Never in the repo (L7) |
| A5 | Code-signing certificate | Makes malware look genuine; revocation damages the product | HSM or hosted signing; environment only (`build.ps1`) |
| A6 | Customers' job inputs and outputs (container tier) | **Not confidential** from the owner on this tier (plan 4.4) | Inside the distro |
| A7 | The host's availability and reputation | A hijacked host damages marketplace trust | Control plane |
| A8 | GPU, CPU, disk, bandwidth of the owner | Mining and abuse; owner's own use | Windows side |
| A9 | The runtime rootfs and tool versions (pinned) | A poisoned image runs every job | Signed runtime manifest (`windows/runtime/manifest.py`) |
| A10 | Release metadata (manifests, ring assignments) | Wrong manifest or ring means a bad build reaches hosts | `agent_releases`, `agent_ring_assignments` |

## 3. Trust boundaries (plan 4.4, made explicit)

| # | Boundary | Statement | Mechanism |
|---|---|---|---|
| B1 | Owner (Windows) vs job | The owner is **not trusted for confidentiality** of jobs; the WSL2 VM memory is readable from Windows. Hence no privacy tier on Windows | X5, capability flags (X4) |
| B2 | Job vs Windows host | A job must never see a Windows file, drive, PATH or interop | `automount` off, `interop` off, `appendWindowsPath` off (X6), non-root containers, `--read-only`, caps dropped, no host mounts, no privileged (ADR-0003) |
| B3 | Job vs LAN | Default no network; egress only through the pinned allow-list proxy | B5 egress proxy; Windows Firewall: outbound allow for the supervisor's interpreter only, no inbound rule |
| B4 | Supervisor vs runner | Authenticated local channel, random per-install token, no remote listener | W4 control channel (another package) |
| B5 | Windows user vs service | A standard local user must not be able to read the credential, write the service's files or change what it runs | ACL on `%ProgramData%\ComputeCoin` (SYSTEM + Administrators), `Program Files` ACL, DPAPI |
| B6 | Fleet vs release pipeline | A host runs only a build signed by the pinned update key | Ed25519 manifest, hash, rings, rollback |
| B7 | Control plane vs host | The API cannot make a host run unsigned code; it can only withhold or choose among signed releases | Offline signing, public key only on the API |
| B8 | Interactive session vs service (session 0) | Activity signals come from the user's session through a helper | `governance/snapshot.py` |

## 4. Data flow summary

```
 release engineer --(offline key)--> signed manifest --> API (verifies, stores) --> host (verifies again, hash, install, health, rollback)
 Windows owner <-> user-session helper --(snapshot JSON)--> supervisor (service) --(token, loopback)--> runner in distro --> job containers
 supervisor --(HTTPS, machine HMAC)--> API        MSI (signed) --> msi_actions.py (SYSTEM) --> service, firewall rule, cleanup
```

## 5. STRIDE

| Component | S: Spoofing | T: Tampering | R: Repudiation | I: Information disclosure | D: Denial of service | E: Elevation of privilege |
|---|---|---|---|---|---|---|
| **MSI / installer** | Fake installer from a look-alike site: Authenticode signature, winget identity | Payload swapped before signing: build from a clean tree, `build.ps1` refuses secret-like files in the payload | Installer log with the consent flag (`LicenseAccepted`) | Payload must carry no secret (L7); scan in `build.ps1` and a test | Failing check aborts and rolls back cleanly | Deferred actions run as SYSTEM: bundled interpreter only, `-I`, fixed argument lists; `Program Files` is admin-writable only |
| **Updater (client)** | Forged manifest: Ed25519 against a **pinned** key, platform and ring checked, signature before fields | Artifact swapped: size + SHA-256 from the signed manifest, hashed again right before install; installer must also verify Authenticode (injected) | Every result code is stable and logged; failures recorded in state | Manifest is public by design; no secret in it | Bad release bricking hosts: rings, health check, automatic rollback, cooldown, no update during a job | The work dir and state file decide what a rollback installs: they must be SYSTEM/Administrators-only (`ensure_private_dir` refuses otherwise) |
| **Release API** | Admin identity: existing RBAC (`administrator`), audit log on every action | Stored manifest is immutable (ORM guard); the API holds only the public key and re-verifies before storing | `record_audit` on publish, pause, resume, rollback, ring assignment | Release list is admin-only; the host route needs machine HMAC | A compromised admin can pause or roll back everything (cannot ship code); alert on rollback | A compromised API cannot sign; it can only choose among already-signed manifests |
| **Supervisor (service)** | Another process posing as the supervisor to the runner: token channel (B4) | Config and secrets under the restricted directory; DPAPI | Event log and file log, no secret values (L7) | Credential only in DPAPI and in memory; never in logs or heartbeats | Service stop timing; drain before stop; governance never kills a job except on `stop now` | Service account choice (LocalSystem is powerful); least privilege is an open decision (see 7.4) |
| **Control channel** | A local process connecting to it: random per-install token, loopback only, versioned messages | Message injection: authenticated and schema-checked | `job_started` and `job_finished` are logged both sides | Token not logged, not in argv | Flooding the channel: bounded queues (check in review) | `set_limits` and `stop` are privileged: token required |
| **Runtime distro** | A swapped rootfs: pinned by digest, signed runtime manifest (X10) | Escape to rewrite the distro: non-root runner, no SSH, no published ports | Container logs per job | Job sees no Windows file (X6) | Disk and memory caps, health checks | Docker group inside the distro is root-equivalent there: the runner is the only member |
| **Governance probes** | A fake snapshot line from another process: the helper's channel must be authenticated (open, see 7.5) | Stale or malformed snapshot: strict decode, freshness check, blocks claims | Decision `code` and `reason` reported in `availability_detail` | Foreground app name is sent only as the process name, never the title or path | A failing probe blocks claims (fail closed) rather than disturbing the owner | The helper runs as the user, not as SYSTEM |

## 6. Abuse cases

Each case: how it would go, what stops it today, what is left, what proof is required before sign-off.

### 6.1 A job escapes to Windows files

* **Attack.** A job exploits a container or kernel bug, or abuses a mount or `/mnt/c` path, to read or write the owner's files or run Windows binaries.
* **Controls (design).** X6 distro hardening (automount off, interop off, PATH not appended); containers non-root, read-only root, capabilities dropped, seccomp, no host mounts, no privileged mode; no Windows drive visible in the distro at all; egress allow-list.
* **Residual.** A container escape lands in the distro, not on Windows; a **WSL2 VM escape** is out of our control (Microsoft's boundary). WSL2 shares one VM per user: a dedicated distro helps, but another distro of the owner could share the VM (documented limit). Memory of the VM is readable by the owner (B1).
* **Proof required.** W11.2 isolation suite on real hardware: read `C:\`, call `cmd.exe` through interop, reach the LAN and the host's loopback services; verify `wsl.conf` is applied after a distro reset. Not yet run.

### 6.2 Malicious or broken update

* **Attack.** (a) Attacker serves a forged manifest or artifact (compromised CDN, DNS, API); (b) a legitimate but bad release; (c) a downgrade to a vulnerable version; (d) replay or freeze of an old manifest; (e) tampering with the cached rollback artifact.
* **Controls.** (a) Ed25519 against a pinned key (verified before any field is read), https only, no redirects, SHA-256 and size, hash re-check before install, Authenticode check injected into the installer step, no credentials in the URL; the API cannot sign. (b) Rings, N consecutive healthy checks that also require the new version to be the running one, automatic rollback with a hash-checked previous artifact, failure cooldown, never during a job. (c) A downgrade is refused unless a signed manifest lists the running version in `revoked_versions`. (e) The work directory must be private (`ensure_private_dir`), and the recorded hash is checked again before a rollback install.
* **Residual.** (d) A freeze attack (serving an old valid manifest forever) is only bounded by the optional `expires_at`; use it. The first update after a fresh install has no cached previous artifact (rollback gap, see installer doc). Key compromise of A4 is catastrophic: needs an HSM, a key-rotation procedure (two pinned keys are supported) and a revocation story. A valid signature is checked, but nothing yet checks the **Authenticode** signature in the shipped `MsiInstaller` unless the supervisor injects `verify_signature`: make it mandatory in review.
* **Tests.** Written: bad signature, tampered field, swapped hash, wrong key, no pinned key, expired, bad URL, size and hash mismatch, replaced-after-check, failed health check, failed install, unavailable previous artifact, tampered previous artifact, cooldown, downgrade, ring rules, floor. Bug re-insertion not done.

### 6.3 Credential theft from the user profile

* **Attack.** Malware or another local user reads the machine credential (`msk_...`) or the control token from disk, then impersonates the host or drives the runner.
* **Controls (design).** DPAPI (X7); `%ProgramData%\ComputeCoin` restricted to SYSTEM and Administrators; credential never in the user profile, never in `agent.toml` after enrolment (migration deletes the old plain file); never logged (L7); heartbeats are signed with an HMAC, so only derived signatures leave the host.
* **Residual.** DPAPI protects against other users, **not** against code running as the same account (LocalSystem or the service account). An administrator can read everything. Between `enroll_remote` writing the file and the scrub, the secret is on disk inside the restricted directory (documented by the supervisor package). Uninstall removes the blobs and Credential Manager entries named `ComputeCoin...`; a copy in a backup or a shadow copy is not covered.
* **Proof required.** File scan of a freshly enrolled install (no plain secret anywhere), ACL listing, and a "standard user cannot read" test on Windows.

### 6.4 Local privilege escalation through the service

* **Attack.** A standard local user abuses the service, the installer or the updater to run code as SYSTEM: writable service binary or directory, unquoted service path, DLL or Python module planting, a writable update cache, a writable `msi_actions.py`, a loopback control port that accepts any local client.
* **Controls.** Files live under `Program Files` (admin-writable only); the MSI custom actions use the bundled interpreter with `-I` (no `PYTHONPATH`, no current-directory or user-site imports) and absolute quoted paths with no trailing backslash before a quote; the update cache and state live under the restricted data directory and the updater refuses an unprotected work dir; the control channel needs a token; no inbound firewall rule.
* **Residual and open questions.** The service account is LocalSystem by default: a bug in the supervisor is then a SYSTEM bug. A virtual account or a dedicated low-privilege account would be better, but WSL2 distros are per user (open design question W2/W3). pywin32's `pythonservice.exe` loads Python from the bundled directory: verify its ACL and search path. The session helper runs as the user and feeds the service: the service must treat its input as hostile (strict decode, freshness) which it does, but the helper's channel is not yet authenticated (7.5). The MSI is perMachine and runs elevated: a repair or upgrade must not run files from a user-writable location.
* **Proof required.** Independent review plus an automated check of the installed tree's ACLs and of the service registration (path quoted, binary owned by SYSTEM or TrustedInstaller or Administrators).

### 6.5 Rogue supervisor or runner channel

* **Attack.** A local process connects to the control channel, or poses as the supervisor, to start or stop jobs, change limits, steal job data or exfiltrate the token; or a job in the distro talks to the channel.
* **Controls (design, owned by W4).** Loopback or distro-address socket only, per-install random token, versioned messages, no LAN listener; the runner keeps working without a supervisor on Linux; jobs have no network by default and cannot reach the host's interfaces.
* **Residual.** Which interface the channel binds in a WSL2 NAT setup, and whether Windows Firewall treats the distro's vEthernet as loopback, is unverified. The MSI deliberately adds **no inbound rule**, so if the channel needs an inbound allow from the WSL network, that rule must be created by the owner of the channel, scoped to the distro's subnet and the supervisor's program.
* **Proof required.** Port scan from the LAN and from a container; connect with a wrong token; replay and malformed messages.

### 6.6 Antivirus false positives (and the opposite: real miners)

* **Attack and risk.** The supervisor or runtime looks like a miner or a remote-access tool (GPU compute, a service that downloads and runs code, a bundled interpreter) and is quarantined, which breaks installs and trust; conversely an attacker uses our host program to run a miner.
* **Controls.** Authenticode-signed MSI and binaries, a transparent consent text, no obfuscation, no process injection, no persistence tricks beyond a documented service, the allow-list egress proxy, existing abuse signals (process names the supervisor can see for the distro, GPU use patterns); vendor submission before each release; the updater never runs unsigned code.
* **Residual.** Unsigned or low-reputation certificates trigger SmartScreen and engines regardless; an EV or hosted-HSM certificate helps. A bundled Python interpreter in an MSI is a common heuristic hit.
* **Proof required.** Scan the release artifacts with the major engines before publishing; keep a submission log; a rollout ring (canary) catches an engine that starts flagging.

## 7. Findings from this work package (things the code review of W5/W6 surfaced)

1. **API `maintenance` is sticky** (`agents.py` heartbeat owner_hold). An agent that reports `maintenance` would be held there after the cause clears. Governance therefore reports `throttled` for every non-online state and carries the detail in `availability_detail`. Server follow-up (W1/W10): store and show that field.
2. **Session 0.** The service cannot see idle time, the foreground window or full-screen state; a helper in the user's session is required (section 3.3 of the installer doc).
3. **Rollback depends on a trusted cache.** The recorded hash and the cached file sit in the same directory; protecting that directory (ACL) is a security requirement, enforced by `ensure_private_dir` only if the supervisor passes it.
4. **Service account** is open (LocalSystem vs a dedicated account; WSL2 distros are per user).
5. **Session helper channel is unauthenticated.** Any local process able to write the snapshot source could claim "owner away" and make the host take the GPU while the owner plays. Authenticate it with the control token or a named pipe with an ACL for the user and SYSTEM only.
6. **Consent is a property.** `LicenseAccepted=1` on a silent command line is an honour system for scripted installs; winget shows the agreement first. Acceptable, but record it in the installer log.
7. **Package name collision.** The new package `computecoin_agent/platform/` shadows the standard library `platform` module for any script that puts that directory first on `sys.path` (running a file from inside `computecoin_agent/` or `python -c` with that directory as the current directory). Always run with `-I`, from the repository root, or via `python -m`.
8. **No `Authenticode` verification in the updater by default.** `MsiInstaller` needs an injected `verify_signature`; make it required, not optional, when the supervisor wires it.

## 8. Security checklist for the independent review (W11.3)

Mark each item with evidence (file and line, log, or recorded run). An unchecked item blocks Windows hosts in production (S8).

### 8.1 Installer and signing
- [ ] MSI and every shipped `.exe` and `.dll` are Authenticode-signed with a timestamp; `signtool verify /pa` output attached.
- [ ] Certificate and update key are not in the repository, images, logs or CI output (`git log -p` scan, CI variable review); `build.ps1` reads only the environment.
- [ ] The payload carries no secret (`build.ps1` refuses secret-like files); `Program Files\ComputeCoin` ACL is inherited from `Program Files` (admin-write only); listing attached.
- [ ] Custom actions: all deferred, `Impersonate="no"`, bundled interpreter, `-I`, quoted paths, no trailing backslash before a quote, no user-writable inputs.
- [ ] Silent installs need `LicenseAccepted=1`; the consent screen text equals `consent.txt` equals the winget Agreements text.
- [ ] Uninstall on a clean VM leaves no service, distro, firewall rule, Credential Manager entry or data directory (W6.3 residue check); upgrade keeps data and credentials.
- [ ] The installer adds **no inbound** firewall rule and starts nothing on a fresh install.
- [ ] The requirements checker runs by module path and a failing check rolls the install back.

### 8.2 Updater and release pipeline
- [ ] `PINNED_PUBLIC_KEYS` holds the real key(s) from the key ceremony; `AGENT_RELEASE_SIGNING_PUBLIC_KEY` on the API equals it; the private key is in an HSM or offline.
- [ ] Manifest signature is verified before any field is read, on the agent and on the API; both use the same prefix and canonical encoding (a test pins this).
- [ ] No update starts while a job runs (gate), including a forced one; no job starts during an update.
- [ ] Downgrades are refused unless the signed manifest revokes the running version; `expires_at` is used for every release.
- [ ] Hash re-check immediately before install and before a rollback install; `ensure_private_dir` is passed by the supervisor and verified on Windows.
- [ ] The injected installer verifies the Authenticode signature of the MSI (`verify_signature` is mandatory in the wiring).
- [ ] Health check includes the running version, requires consecutive successes and a timeout; a rollback that does not come back healthy blocks the host and alerts.
- [ ] Rings: the server decides the ring; a host can only widen it; canary soak before early and general; alerting on `rolled_back` and `rollback_failed`.
- [ ] Forced floor is used only for security fixes; a host below it claims nothing.
- [ ] Admin release routes are administrator-only and audited; a rolled-back release cannot be resumed.

### 8.3 Supervisor, secrets and control channel
- [ ] Credentials only in DPAPI or Credential Manager; ACL on `%ProgramData%\ComputeCoin` is SYSTEM and Administrators only; no plain `msk_` anywhere (file scan on a freshly enrolled host).
- [ ] The service account is decided and least privilege is justified in writing; service path quoted; service binary not writable by users.
- [ ] Control channel: loopback or distro-only bind, token required, constant-time compare, bounded message size and queue, versioned schema; port scan from LAN and container.
- [ ] Session helper channel is authenticated; the supervisor treats snapshots as hostile (strict decode, freshness) and fails closed.
- [ ] Logs contain no secret values and no foreground window titles; foreground app names are limited to process names.
- [ ] Governance defaults are conservative; `stop now` works while a job runs; probes failing never open the gate.

### 8.4 Runtime distro and job isolation (needs real hardware)
- [ ] `wsl.conf` applied: automount off, interop off, `appendWindowsPath` off; verified from inside a job container after a reset.
- [ ] A job cannot read `C:\`, run Windows executables, or reach the host's loopback services or the LAN (W11.2).
- [ ] Rootfs pinned by digest; Docker Engine and NVIDIA toolkit at pinned versions; runner user non-root; no SSH; no published ports.
- [ ] Containers: read-only root, capabilities dropped, no new privileges, no host mounts, no privileged mode (ADR-0003 escape suite on the Windows runtime).
- [ ] Disk and memory caps enforced; the owner's `.wslconfig` is not edited without consent.
- [ ] Windows hosts never receive private-tier, BYOC, gVisor or microVM jobs (X5, W1.3).

### 8.5 Abuse and AV
- [ ] Release artifacts scanned by major engines; submission log kept; canary ring used for each release.
- [ ] Abuse signals (process names, GPU patterns) work for the Windows runtime and are reported without secrets.
- [ ] Support bundle is redacted (W10.2).

### 8.6 Supply chain
- [ ] Python dependencies of the bundled runtime pinned and hash-checked; SBOM produced per release.
- [ ] Build runs from a clean checkout on a trusted builder; the signed MSI hash recorded in the manifest equals the artifact served.
- [ ] Every external interface used is recorded with URL and date (S4).
