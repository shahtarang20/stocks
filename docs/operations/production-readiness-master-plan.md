# Production readiness: master plan, master prompt and master checklist

_Status date: 2026-10-29 · Owner: engineering · Status: **plan as written at the baseline; implementation in progress, see note** ·
Baseline: `main` at `33e2c00` (API 671 passed / 41 skipped, agent 138, Python SDK 76,
TypeScript SDK 22, guardian + witness 28, e2e 10, web 22, contracts 33)._

> **Implementation note (2026-11-05).** P1 to P5 now have code in the working tree (uncommitted). Statically reviewed only: no checklist item in this plan is ticked, because a tick needs its proof (a passing test run, or for **(hardware)** items a recorded file from a real machine, R8) and none of that has been attached yet. Hardware, live-chain and GPU paths have not been run. Read the checklists below as the acceptance list still to be proven, not as a record of what exists. The text of the plan is kept as written at the baseline; where it says "today" or "not built", it describes the baseline.

This document turns the six pieces still missing for a **real production launch** into work
packages that any engineer or coding agent can execute and that a reviewer can verify item by
item. The demo launch does not depend on any of them.

| Read first | Why |
|---|---|
| [ADR-0005 Proof-of-Privacy](../architecture/ADR-0005-proof-of-privacy.md) | Tiers, guardians, verdicts, receipts |
| [ccpp-v1 spec](../specs/ccpp-v1.md) sections 6a–6f | Wire formats this work extends |
| [proof-of-privacy-remaining-work.md](proof-of-privacy-remaining-work.md) | How the previous work was done and verified |
| [amd-gpu-support-plan.md](amd-gpu-support-plan.md) | Locked decisions D1–D9 (D5 matters for P6) |
| [ADR-0003 isolation](../architecture/ADR-0003-isolation.md) | Container hardening rules every image must respect |

---

## 1. Scope

| WP | What | Code? | Fully provable in this repo? | Needs beyond code |
|---|---|---|---|---|
| **P1** | Stake sync: on-chain `ComputeStakeVault` → `provider_machines.stake_bonded` | Yes | **Yes** (fake RPC + local Hardhat chain) | Nothing |
| **P2** | Hardware-proof hardening: certificate revocation (Intel, AMD, NVIDIA), Intel TDX-module identity, NVIDIA signed reference measurements (RIM) | Yes | **Mostly** (vendors publish CRLs, TCB Info and RIMs) | Final check with real evidence (P4 machines) |
| **P3** | Real AI workload images (CUDA + ROCm) and the real inference path | Yes | Logic yes; real output needs a GPU | One NVIDIA GPU host, one AMD GPU host |
| **P4** | Agent collects SEV-SNP, Intel TDX and NVIDIA GPU evidence | Yes | With synthetic / recorded evidence | A confidential VM (SEV-SNP or TDX) with an NVIDIA H100 in CC mode |
| **P5** | Sealed OS image (measured boot) and its trusted-image record | Yes (build pipeline) | Boot-tested in QEMU + software TPM | One real TPM 2.0 PC to record final PCRs |
| **P6** | AMD GPUs in the Sealed tiers | Yes (after a decision) | With software TPM | Owner decision (changes D5); real AMD + TPM host |

**Order:** P1 → P2 → P3 → P4 → P5 → P6. P1 and P2 need no hardware. P3 is the biggest value
(real outputs). P4–P6 finish when the machines are available.

**Not code (tracked elsewhere):** deployment, testnet BNB, audit, legal, Safe multisig, final
tokenomics, real BNB liquidity ([launch-checklist.md](launch-checklist.md)).

---

## 2. Locked decisions

| ID | Decision |
|---|---|
| **R1 Fail closed** | Anything not proven is refused: no stake read = previous stake kept and flagged, no fresh CRL = no verdict, no image for a runtime = machine skipped, no evidence = no tier. Never a silent default that grants something. |
| **R2 One agent at a time** | Exactly one coding agent edits this repository at a time (Codex, Claude, any other). Before every edit, check the file's modification time has not changed since it was read; if it has, STOP. |
| **R3 Money code untouched** | No change to `contracts/`, ledger, pricing, billing or settlement code. P1 only **reads** the vault. |
| **R4 Chain is the source of truth for stake** | When stake sync is on, `stake_bonded` comes only from the vault; the admin "record stake" route refuses (409 `stake_managed_on_chain`). |
| **R5 Collateral is trusted by signature, not by origin** | CRLs, TCB Info, QE Identity and RIMs are accepted only after their signature chains to a **pinned** root (`PRIVACY_HARDWARE_ROOT_CERTS`). Where they were downloaded from never matters; the host may supply them. |
| **R6 No network inside job containers** | Model weights are baked into the workload image, pinned by digest. The image digest IS the model identity (`check_model_identity`). |
| **R7 Verify every external interface** | Every vendor URL, file format, tool and API used (Intel PCS, AMD KDS, NVIDIA RIM/OCSP, configfs-tsm, snpguest, nvtrust, vLLM) is checked against the vendor's current documentation **before** use, and the doc URL + date is recorded next to the code. Memory of an API is not evidence. |
| **R8 Hardware-gated items need recorded evidence** | Items marked **(hardware)** are never ticked without a recorded file from a real machine committed under `tests/fixtures/hardware/` and a passing test that uses it. |
| **R9 Existing tests stay** | An existing test changes only when a locked decision requires it, and the reason is written in the commit. |
| **R10 No secrets anywhere** | No private key in code, images, fixtures, logs or test output. Hardhat's public dev key is never used; tests use fresh random keys. |
| **R11 D5 is the owner's decision** | P6 changes AMD design decision D5 (container only). STOP and ask the owner before any P6 code. |
| **R12 Determinism for verification** | Workload images produce byte-identical output for identical input on the same runtime (greedy decoding, fixed seed), so shadow re-runs and challenges keep working (per-runtime hashes, as built). |

---

## 3. Master prompt

Copy everything inside the box into the agent.

~~~text
MASTER PROMPT: ComputeCoin production readiness (P1–P6)

ROLE
You are a senior engineer working in the repository at /home/root509/stocks. The plan is
docs/operations/production-readiness-master-plan.md. Read it completely, including the locked
decisions R1–R12 in section 2. Follow them exactly. If one looks wrong, STOP and ask the owner.

GROUND RULES
1. Work ONLY inside /home/root509/stocks. Never touch another project. Never install anything
   system-wide. Tools you need go into the git-ignored /home/root509/stocks/.tools/ folder (the
   way the software TPM was unpacked there with `apt download` + `dpkg-deb -x`), or into the
   project's own virtualenvs / node_modules.
2. One agent at a time (R2). Before starting: confirm no other agent is editing. Before EVERY
   edit: compare the file's modification time with when you read it. If it changed, STOP and report.
3. Before your first edit, snapshot:
   git update-ref refs/snapshots/before-<wp> $(git stash create "before <wp>")
4. One work package at a time, in order P1, P2, P3, P4, P5, P6. P6 needs the owner's decision
   first (R11).
5. Every behaviour change ships with a test that FAILS without it. After each package, prove it
   with bug re-insertion: temporarily remove each new check, confirm a named test fails, restore
   the file, and confirm with checksums that the restore is byte-identical.
6. After each package run ALL suites and compare with the baseline in this document's header:
   - API:            cd apps/api && .venv/bin/python -m pytest -q
   - Agent:          cd provider-agent && .venv/bin/python -m pytest -q
   - Python SDK:     cd sdk/python && ../../apps/api/.venv/bin/python -m pytest -q tests
   - TypeScript SDK: cd sdk/typescript && npx vitest run && npx tsc --noEmit
   - Guardian/witness: apps/api/.venv/bin/python -m pytest -q apps/guardian apps/witness
   - E2E:            apps/api/.venv/bin/python -m pytest -q -rs tests/e2e
   - Web:            cd apps/web && npx vitest run && npm run typecheck && npx next lint && npm run build
   - Contracts:      cd contracts && npx hardhat test
7. Tick a checklist item in section 5 ONLY with its proof written next to it (test name,
   command output or file). Hardware items need recorded evidence (R8).
8. Do not commit or push unless the owner says so. If on main, create a branch first.
9. After each package, write the report (section 7) and WAIT for review.

TRAPS ALREADY HIT IN THIS CODEBASE (do not repeat them)
- Async SQLAlchemy cannot lazy-load: never touch relationship attributes (machine.hardware,
  model_version.images) on objects fetched without eager loading. Query instead (see
  app/services/gpu_runtime.py: gpu_runtime_of_machine, model_image_runtimes, image_for_runtime).
- Raising HTTPException rolls back the request's writes; returning a JSONResponse keeps them.
  Use the return form when a failure itself must be recorded (attempt counters, failed jobs).
- Never read a security claim from an unsigned field next to a signed blob: parse the signed bytes.
- A mock that copies the code's assumptions proves nothing: mocks follow the vendor's documented
  API (R7).
- Tests must not use the network, the user's home directory or system temp for state that
  matters; keep scratch under the project (.tools/) or pytest's tmp_path.

WORK PACKAGES (details and acceptance in sections 4 and 5 of the plan)
P1 Stake sync          P2 Revocation, TDX module, NVIDIA RIM     P3 Workload images + real inference
P4 Agent evidence      P5 Sealed OS image                        P6 AMD in Sealed tiers (ask first)

REPORT AFTER EACH PACKAGE (section 7 format)
Files changed (one line each on why), tests added, all suite counts before/after, bug
re-insertion results, checklist items ticked with proof, items not done and why, external docs
consulted (URL + date), open risks.
~~~

---

## 4. Work packages

### P1 Stake sync (vault → database)

**Goal.** A machine's bonded stake in the database always equals what the vault says, so
privacy tiers and the value-at-risk gate (`MIN_SEALED_STAKE` 1000, `MIN_SEALED_PRO_STAKE` 5000 in
`apps/api/app/services/attestation_verifier.py`) rest on real, slashable stake.

**Facts (verified 2026-10-29).**
- `contracts/src/ComputeStakeVault.sol`: `mapping(bytes32 => MachineStake) public stakes` with
  `MachineStake{provider, bondedAmount, unbondingAmount, unbondingAvailableAt, isBanned}`; events
  `StakeDeposited`, `UnbondingRequested`, `StakeWithdrawn`, `MachineSlashed`; `registerMachine(bytes32, address)` is owner-only.
- Database: `ProviderMachine.stake_bonded` is `Numeric(18, 2)` in token units; the token has 18 decimals.
- Today stake is recorded by hand: admin route in `apps/api/app/api/routers/privacy.py` (`StakeBondRequest`).
- Job IDs map to `bytes32` with `keccak(text=str(uuid))` (`settlement.job_id_to_bytes32`).
- The chain-reading pattern to reuse: `apps/api/app/services/chain_reconciler.py`
  (injectable JSON-RPC client; "I could not check" is never "it matched").

**Design.**
1. `machine_id_to_bytes32(machine_uuid) = keccak(text="computecoin-machine:" + str(uuid))`. The
   domain prefix keeps machine IDs from ever colliding with job IDs. Put it next to
   `job_id_to_bytes32` and document it in the spec.
2. `app/services/stake_sync.py`: for each machine, read `stakes(bytes32)` with `eth_call` **at
   block `latest - STAKE_SYNC_CONFIRMATIONS`** (default 15) so a reorg cannot grant stake.
   Bonded = `bondedAmount` only (unbonding stake does NOT count). Convert wei → token units
   rounding **down** to 2 decimals. Also: vault `isBanned` → `machine.is_banned = True` (never
   un-bans automatically).
3. Provider address check: the vault's `provider` address must be one of the machine owner's
   `LinkedWallet` addresses (`apps/api/app/models/wallet.py`) on the vault's chain id. A mismatch
   (or no linked wallet) marks the machine `stake_unverified` and its stake counts as 0 (R1), so
   nobody can claim stake someone else deposited.
4. Every sweep writes a `StakeSyncRun` row (machines checked, changed, errors), like
   `ChainReconciliationRun`. An RPC failure keeps the previous value and records the error; it never zeroes or raises stake.
5. Worker: `app/worker.py` `sweep_once` gains a stake-sync step with its own interval and its own
   try/except (a chain outage must not stop lease recovery, as for anchoring).
6. Settings: `STAKE_SYNC_ENABLED`, `STAKE_SYNC_RPC_URL`, `STAKE_SYNC_CHAIN_ID`,
   `STAKE_SYNC_VAULT_ADDRESS`, `STAKE_SYNC_INTERVAL_SECONDS` (300), `STAKE_SYNC_CONFIRMATIONS`
   (15). Boot guard: all required when enabled outside dev; chain id must match the RPC.
7. R4: with sync enabled, the admin stake route returns 409 `stake_managed_on_chain`.
8. A lowered stake takes effect immediately: a machine below `MIN_SEALED_STAKE` gets no new
   Sealed verdicts and the scheduler's value-at-risk gate uses the new value.

**Tests.** Fake RPC: deposit, unbond (bonded drops), withdraw, slash, ban, provider mismatch,
RPC error keeps the old value, confirmations respected, rounding down. Worker isolation test.
Live opt-in test `RUN_STAKE_SYNC_INTEGRATION=1`: local Hardhat chain, real token + vault
deployed from artifacts with fresh random keys (pattern: `apps/api/tests/test_log_anchor_live.py`),
deposit → sync → Sealed verdict allowed; unbond → sync → refused.

### P2 Hardware-proof hardening

**Goal.** The TDX, SEV-SNP and NVIDIA verifiers (`apps/api/app/services/hardware_attestation/`)
also reject revoked certificates, an unknown TDX module, and GPU measurements that differ from
NVIDIA's signed reference values.

**Design.**
1. **Revocation (R5).** Evidence may carry the issuer's CRLs. The verifier accepts a CRL only if:
   it is signed by the CA that issued the certificate in question; `thisUpdate ≤ now < nextUpdate`;
   it covers that issuer. Any certificate in the path whose serial is listed is refused
   (`*_certificate_revoked`). A certificate path with no current CRL for an issuer that publishes
   one is refused (`*_revocation_unknown`), never accepted.
   - Intel: PCK CRL (processor / platform CA) and Root CA CRL from Intel PCS.
   - AMD: the CRL for the ARK/ASK chain from AMD KDS.
   - NVIDIA: OCSP responses for the device chain (signed by NVIDIA's OCSP responder chaining to the pinned root), or CRL where published.
   Verify each endpoint and format against current vendor docs first (R7).
2. **TDX module identity.** From the Intel-signed TCB Info (v3, already verified in `tdx.py`):
   check the quote's `MRSIGNERSEAM` and `SEAMATTRIBUTES` against the published TDX module
   identity, and the module's TCB level from `TEE_TCB_SVN` against its TCB levels (UpToDate only).
   Replace the "Not implemented: TDX-module identity" note.
3. **NVIDIA RIM.** Parse NVIDIA's Reference Integrity Manifests (signed SWID/XML) for the GPU
   driver and VBIOS: verify the XML signature chains to a pinned NVIDIA RIM root, then compare
   every measurement index the RIM defines with the signed SPDM measurement blocks (any listed
   alternative may match). The golden GPU values then come from RIMs, not from trusted-image
   configuration. XML signature checking uses a maintained library with a pinned version.
4. Spec: extend ccpp-v1 6f with the collateral fields; update the "Not implemented" lists in
   `tdx.py`, `nvidia.py`, ADR-0005 status.

**Tests.** Revoked leaf / intermediate; CRL from the wrong issuer; expired CRL; missing CRL;
forged CRL signature; wrong TDX module signer; TDX module below UpToDate; RIM with a forged
signature; RIM mismatch on one index; RIM alternative match. Use **real published** CRLs, TCB
Info and RIM files as fixtures where possible (downloaded once, stored under
`apps/api/tests/fixtures/vendor/` with their SHA-256 and source URL recorded); treat them as
untrusted data (parse only, never execute).

### P3 Real workload images and the real inference path

**Goal.** Jobs and the OpenAI-compatible API return real model output from real GPUs, with the
image digest proving which model ran.

**Facts (verified 2026-10-29).**
- Workload contract in `provider-agent/computecoin_agent/executor.py`: the container reads
  `COMPUTECOIN_INPUT` (`/out/input.json`), writes `COMPUTECOIN_OUTPUT` (`/out/output.json`), and gets
  `COMPUTECOIN_JOB_ID`, `COMPUTECOIN_MODEL`, `COMPUTECOIN_WORKLOAD_TYPE`; output is checked by
  `validate_output(workload_type, output)`; containers run hardened (no network, read-only, non-root).
- Images per runtime: `ModelVersionImage(model_version_id, gpu_runtime, image_digest)`;
  `model_versions.image_digest` is the CUDA build (D4).
- `apps/api/app/services/inference.py` always returns simulated output ("Hardware path — future").
- The seeded hardware model is `tinyllama-1.1b-chat-v1` (`apps/api/tests/helpers.py seed_hardware_model`).

**Design.**
1. `workloads/text-generation/`: one entrypoint (Python) implementing the contract, plus
   `Dockerfile.cuda` and `Dockerfile.rocm` (vLLM base images **pinned by digest**, verified per
   R7), weights for **TinyLlama-1.1B-Chat** baked in at build time with their SHA-256 recorded
   (R6), non-root user, greedy decoding with a fixed seed (R12), no network at run time.
   `workloads/embeddings/` likewise with a small embedding model.
2. Build pipeline: a CI workflow builds both images, pushes to a registry (GHCR), and writes the
   digests to `workloads/digests.json`. An admin-only API (or migration-safe script) registers
   them as `ModelVersionImage` rows; the image digest is also logged as an `IMAGE_HASH`
   transparency-log entry.
3. Real inference path: `run_chat_completion` / `run_embedding` for a non-simulated model create a
   real job (placed by the scheduler through `is_gpu_compatible`), wait for it with a timeout
   (`INFERENCE_TIMEOUT_SECONDS`), bill it through the normal job billing, and map `output.json`
   to the OpenAI response. `computecoin.execution_mode = "hardware"`, `image_digest` = what ran.
   On timeout: 504 and the job is cancelled with its hold released. Simulated models keep today's path.
4. Verification: register challenge answers per runtime (`expected_hashes_by_runtime`) for the
   new images, produced by running each image once on real hardware (hardware step).

**Tests.** Entrypoint contract test runs the entrypoint in-process with a tiny fake model
(no GPU). Inference path: simulated executor end to end (job created, billed, output mapped,
timeout cancels and refunds). Image digests registered per runtime are what the claim hands out
(existing AMD tests extended). Opt-in `RUN_GPU_TESTS=1`: real container on a real GPU, output
valid, two runs byte-identical (R12). **(hardware)** One recorded run per runtime.

### P4 Agent collects SEV-SNP, TDX and NVIDIA evidence

**Goal.** A Confidential host's agent produces exactly the evidence the API's verifiers accept
(ccpp-v1 6f), bound as the API requires.

**Design.**
1. `provider-agent/computecoin_agent/tee.py`, each collector injectable (runner / file paths), like `tpm.py`:
   - **NVIDIA GPU:** SPDM GET_MEASUREMENTS with nonce `SHA-256(challenge)` and the device
     certificate chain, using NVIDIA's attestation tooling (verify per R7) → `{"request", "response", "certificate_chain"}`.
   - **SEV-SNP:** guest report via the kernel interface (`/dev/sev-guest` or configfs-tsm; verify
     per R7) with REPORT_DATA = SHA-512 of the GPU-bound nonce; VCEK + ASK from AMD KDS (or a
     local cache) → `{"report", "vcek_cert", "ask_cert"}`.
   - **TDX:** quote via configfs-tsm (`/sys/kernel/config/tsm/report`) with REPORTDATA = SHA-512 of
     the GPU-bound nonce, plus Intel collateral (TCB Info, QE Identity, TCB signing chain) →
     `{"quote", "collateral"}`.
2. Order for Confidential: GPU evidence first, then the CPU report over
   `gpu_bound_nonce(challenge, gpu_evidence)`. Copy the exact function from
   `app/services/attestation_verifier.py` into the agent and add a cross-check test that both
   produce the same string for the same inputs.
3. `perform_attestation(..., hardware=...)` gets a provider that returns
   `("confidential", {"cpu_flavor", "cpu", "gpu"})`; config: `tee_cpu` (`tdx` | `sev_snp`), `nvidia_cc` on/off.
4. Agent never sends a placeholder: a collector that fails raises, and attestation fails (R1).

**Tests.** Each collector with injected fake devices / runners; the agent's output verified by
the API's real verifiers in one test (round-trip). **(hardware)** One recorded evidence set per
flavor from a real machine under `tests/fixtures/hardware/`, verified by the API.

### P5 Sealed OS image

**Goal.** A reproducible, measured operating system image for Sealed hosts whose expected PCR
values are published, so TPM verdicts mean "this host booted exactly this image".

**Design.**
1. `os/sealed/`: build with a declarative image builder (e.g. mkosi; verify per R7): unified
   kernel image (UKI) with the kernel command line baked in (`lockdown=confidentiality`, IOMMU
   on), dm-verity read-only root, no SSH, no interactive login, the ComputeCoin agent preinstalled,
   Docker with the hardening ADR-0003 requires, Secure Boot signing.
2. Predict PCRs at build time (PCR 4, 7, 11; the UKI tooling can pre-compute PCR 11) and emit
   `os/sealed/out/expected-pcrs.json`. Publish: a `TrustedImage` row (`image_hash`,
   `expected_pcrs`, tier `sealed`) and an `IMAGE_HASH` transparency-log entry.
3. Reproducibility: two builds from the same inputs give the same image hash (CI checks it).
4. Release process: image hash, expected PCRs and build provenance in release notes.

**Tests.** Opt-in boot test: QEMU + OVMF + software TPM (the project-local swtpm from
`.tools/`), boot the image, run `computecoin-agent tpm-enrol` and attest → TPM PCRs equal the
prediction → hardware verdict for tier `sealed` (extends `tests/e2e/test_tpm_swtpm_e2e.py`).
Changing one byte of the image changes PCRs and the verdict is refused. **(hardware)** Boot on a
real TPM PC, record PCRs, compare with the prediction.

### P6 AMD GPUs in the Sealed tiers

**Status: needs the owner's decision first (R11).** Sealed and Sealed Pro rest on the TPM and the
Sealed OS, which are GPU-vendor independent; D5 currently limits AMD GPU jobs to the container
tier. Confidential needs a GPU TEE, which current AMD GPUs do not offer: **Confidential with AMD
GPUs is a hardware limit, not a code task.**

**Design (after approval).** Allow `sealed` / `sealed_pro` for ROCm in
`gpu_runtime.is_gpu_compatible` and the submit check, only on machines whose verdict names a
Sealed image; AMD device passthrough rules (D6) unchanged inside the sealed run; update the
website/SDK tier restriction and ADR-0003 addendum. Tests: ROCm + Sealed allowed with a Sealed
verdict, refused without; ROCm + Confidential still refused. **(hardware)** AMD GPU host booted
from the Sealed OS, enrolled, attested, one sealed ROCm job.

---

## 5. Master checklist

Tick an item only with its proof written after it.

### P1 Stake sync
- [ ] P1.1 `machine_id_to_bytes32` with the `computecoin-machine:` prefix; documented in the spec. *Proof: test + spec diff.*
- [ ] P1.2 Sync reads `stakes(bytes32)` at `latest - confirmations`; bonded = `bondedAmount` only; rounds down. *Proof: tests (deposit, unbond, rounding, confirmations).*
- [ ] P1.3 Vault ban → `is_banned`; never auto-unban. *Proof: test.*
- [ ] P1.4 Vault provider address not among the owner's `LinkedWallet` addresses on that chain → stake counts as 0, machine flagged. *Proof: tests (mismatch, no wallet, other chain).*
- [ ] P1.5 RPC failure keeps the previous value and records the error. *Proof: test.*
- [ ] P1.6 `StakeSyncRun` recorded every sweep; migration upgrade/downgrade works. *Proof: tests.*
- [ ] P1.7 Worker step isolated from lease recovery. *Proof: `sweep_once` test.*
- [ ] P1.8 Settings + boot guard. *Proof: config tests.*
- [ ] P1.9 Admin stake route refuses with sync on (R4). *Proof: test.*
- [ ] P1.10 Lowered stake blocks new Sealed verdicts and the value-at-risk gate. *Proof: tests.*
- [ ] P1.11 Live local-chain test passes. *Proof: `RUN_STAKE_SYNC_INTEGRATION=1` output.*

### P2 Hardware-proof hardening
- [ ] P2.1 CRL acceptance rules (issuer signature, freshness, coverage). *Proof: tests incl. forged and expired CRL.*
- [ ] P2.2 Revoked certificate refused for Intel, AMD, NVIDIA paths. *Proof: tests.*
- [ ] P2.3 Missing revocation data refused (`*_revocation_unknown`). *Proof: test.*
- [ ] P2.4 TDX module identity and TCB level checked. *Proof: tests.*
- [ ] P2.5 NVIDIA RIM signature verified; measurements compared; alternatives honoured. *Proof: tests with real published RIM fixtures.*
- [ ] P2.6 Vendor fixtures stored with SHA-256 and source URL; external docs recorded (R7). *Proof: fixture README.*
- [ ] P2.7 Spec 6f, verifier docstrings and ADR-0005 "not implemented" lists updated. *Proof: diff.*

### P3 Workload images and real inference
- [ ] P3.1 Entrypoint implements the workload contract; contract test passes without a GPU. *Proof: test.*
- [ ] P3.2 `Dockerfile.cuda` and `Dockerfile.rocm` with base images pinned by digest, weights baked in with SHA-256 recorded, non-root, no runtime network. *Proof: files + build log.*
- [ ] P3.3 CI builds and pushes both images; digests recorded in `workloads/digests.json`. *Proof: CI run.*
- [ ] P3.4 Digests registered per runtime and logged as `IMAGE_HASH`. *Proof: test.*
- [ ] P3.5 Real inference path: job created, placed, billed, output mapped; `execution_mode = "hardware"`. *Proof: tests with the simulated executor.*
- [ ] P3.6 Inference timeout → 504, job cancelled, hold released. *Proof: test.*
- [ ] P3.7 Deterministic output (two runs identical). *Proof: `RUN_GPU_TESTS=1` output.* **(hardware)**
- [ ] P3.8 One real run per runtime recorded; challenge answers registered per runtime. *Proof: recorded fixtures.* **(hardware)**

### P4 Agent evidence
- [ ] P4.1 NVIDIA collector produces the verifier's format with nonce `SHA-256(challenge)`. *Proof: round-trip test.*
- [ ] P4.2 SEV-SNP collector, REPORT_DATA over the GPU-bound nonce. *Proof: round-trip test.*
- [ ] P4.3 TDX collector with Intel collateral. *Proof: round-trip test.*
- [ ] P4.4 Agent and API `gpu_bound_nonce` agree. *Proof: cross-check test.*
- [ ] P4.5 A failing collector fails attestation; nothing is faked. *Proof: tests.*
- [ ] P4.6 Recorded real evidence per flavor verifies. *Proof: fixtures + test.* **(hardware)**

### P5 Sealed OS image
- [ ] P5.1 Image definition (UKI, dm-verity root, lockdown, IOMMU, no login, agent, hardened Docker). *Proof: files.*
- [ ] P5.2 Predicted PCRs emitted; `TrustedImage` + `IMAGE_HASH` published. *Proof: build output + test.*
- [ ] P5.3 Reproducible build (same hash twice). *Proof: CI.*
- [ ] P5.4 QEMU + software TPM boot → enrol → attest → Sealed verdict; one changed byte → refused. *Proof: opt-in e2e output.*
- [ ] P5.5 Real TPM PC PCRs equal the prediction. *Proof: recorded PCRs.* **(hardware)**

### P6 AMD in Sealed tiers
- [ ] P6.0 Owner approved the D5 change (date, decision recorded in the AMD plan). *Proof: plan diff.*
- [ ] P6.1 ROCm + Sealed allowed with a Sealed verdict, refused without; ROCm + Confidential still refused. *Proof: tests.*
- [ ] P6.2 Website/SDK tier restriction and ADR-0003 addendum updated. *Proof: diff + web tests.*
- [ ] P6.3 Real AMD host on the Sealed OS runs a sealed ROCm job. *Proof: recorded run.* **(hardware)**

### Cross-cutting sign-off
- [ ] X1 No change under `contracts/`, ledger, pricing, billing, settlement. *Proof: `git diff --stat` vs baseline.*
- [ ] X2 All suites green; counts compared with the baseline in the header. *Proof: outputs.*
- [ ] X3 Bug re-insertion run for every new check; all caught; restores verified by checksum. *Proof: recorded run.*
- [ ] X4 Every external interface recorded with doc URL + date (R7). *Proof: list.*
- [ ] X5 No secret in any file, image, fixture or log (R10). *Proof: scan output.*
- [ ] X6 ADR-0005 status, spec and runbooks updated. *Proof: diff.*
- [ ] X7 Independent code review on the full diff; findings fixed or recorded. *Proof: review report.*
- [ ] X8 Owner approval before merge.

---

## 6. End-to-end acceptance

| # | Scenario | Pass when |
|---|---|---|
| AE-P1 | Host deposits stake on the vault | Within one sync interval the API shows it; a Sealed verdict is allowed; after unbonding it is refused |
| AE-P2 | A revoked certificate in real evidence | Verdict refused with `*_certificate_revoked`; the same evidence with a clean CRL passes |
| AE-P3 | `/v1/chat/completions` on TinyLlama | Real text from a real GPU, `execution_mode: "hardware"`, `image_digest` matches the registered image, the customer is billed for metered seconds |
| AE-P4 | Confidential host (TDX or SEV-SNP + H100 CC) | The agent's evidence yields a Confidential verdict; swapping in another machine's GPU report is refused |
| AE-P5 | Host boots the Sealed OS | Its TPM quote matches the published PCRs and it receives a Sealed verdict; a modified image does not |
| AE-P6 | AMD host on the Sealed OS (after approval) | A sealed ROCm job completes with a valid privacy receipt |

---

## 7. Report format (after each package)

| Section | Content |
|---|---|
| Files changed | One line each on why |
| Tests added | Names, and which failure each one pins |
| Suites | Before / after counts for all suites |
| Bug re-insertion | Each check removed → which test failed → restore verified |
| Checklist | Items ticked with proof; items not done and why |
| External docs | URL + date for every vendor interface used (R7) |
| Risks | What is still uncertain, and what hardware would settle it |
