# AMD GPU Support: Master Prompt and Checklist

_Status date: 2026-10-26 · Owner: engineering · Status: **planned, not started** ·
Companion to [implementation-checklist.md](implementation-checklist.md),
[ADR-0003-isolation.md](../architecture/ADR-0003-isolation.md) and
[gpu-validation-runbook.md](gpu-validation-runbook.md)._

This document adds AMD GPUs (Instinct and Radeon, through ROCm) next to NVIDIA, so one
marketplace serves both vendors. It has five parts:

1. **Summary**: what changes, for whom, and how big the change is.
2. **Locked design decisions**: the choices every work package must follow.
3. **Master prompt**: paste it into a coding agent (or hand it to an engineer) to do the work.
4. **Master checklist**: tick items as they are done; a reviewer signs off at the end.
5. **Out of scope and follow-ups**.

---

## 1. Summary

**Today** the provider agent is NVIDIA-only: detection uses NVML
([hardware.py](../../provider-agent/computecoin_agent/hardware.py)), GPU jobs need the
NVIDIA container toolkit and `--gpus`
([executor.py](../../provider-agent/computecoin_agent/executor.py)), and telemetry reads
NVML ([telemetry.py](../../provider-agent/computecoin_agent/telemetry.py)). An AMD machine
enrolls as CPU-only. The backend is mostly vendor-neutral but has no vendor field.

**After this work**

| Who | What they see |
|---|---|
| NVIDIA provider | Nothing changes. Existing machines are backfilled as `nvidia` / `cuda`. |
| AMD provider | Installs the same agent; the GPU is detected, enrolled, scheduled and paid like NVIDIA. |
| Inference API customer (`/v1/chat/completions`, `/v1/embeddings`) | Nothing to choose. Requests go to the cheapest eligible machine of either vendor, using the image built for that vendor. |
| Custom job customer | Chooses `gpu_runtime`: `cuda` (default, NVIDIA), `rocm` (AMD) or `any` (their image supports both). |

**Rules:** one vendor per job, one vendor per machine, AMD GPU jobs run in the
`container` isolation tier only (v1).

**Size (estimate, not measured from a prototype):** about 900 lines of code and
650 lines of tests; 1½–2½ weeks for one developer including a real-hardware run.

| Area | ~Lines |
|---|---|
| Agent: detection, execution, telemetry, isolation, reporting | 260 |
| Backend: migration, models, schemas, ingest | 90 |
| Backend: scheduler, marketplace, inference routing | 95 |
| Backend: verification (re-runs, challenges) | 75 |
| Frontend | 150 |
| SDKs | 40 |
| Docs | ~200 (not counted above) |
| Tests | 650 |

**Prerequisite for AMD providers to earn anything:** real (non-simulated) inference and
ROCm versions of the model images. The code here makes AMD machines schedulable; it does
not build workload images (the repo has none yet for either vendor).

---

## 2. Locked design decisions

Every work package follows these. Change one only by editing this section first and
getting the owner's approval.

**D1. Field names.** Use `gpu_vendor` (`nvidia` | `amd`), `gpu_runtime` (`cuda` | `rocm`)
and `gpu_runtime_version` (string). Do **not** use a bare `runtime`:
`ModelVersion.runtime` already means simulated vs hardware
([model_registry.py](../../apps/api/app/models/model_registry.py)) and is read by the
scheduler and inference service.

**D2. One vendor per machine.** If the agent detects both NVIDIA and AMD GPUs, it must
be told which to offer (`gpu_vendor` in the agent config). Without that setting,
enrollment fails with a clear message. The backend rejects a capability report that
mixes vendors (422).

**D3. One vendor per job.** Jobs carry `required_gpu_runtime`: `cuda` | `rocm` | `any`.
The default is `cuda`, so every existing client and job keeps today's behaviour. The
inference service sets `any` and chooses the image per machine (D4).

**D4. One image per runtime.** New table `model_version_images`
(`model_version_id`, `gpu_runtime`, `image_digest`; unique on the first two; digest must
be a pinned `sha256`, as today). Existing non-null `ModelVersion.image_digest` values are
backfilled as `cuda` rows; the column stays for compatibility. All reads go through one
helper, `image_for(model_version, gpu_runtime) -> str | None`. A hardware model version
with no image for a machine's runtime makes that machine ineligible for it.

**D5. Isolation for AMD GPU jobs is `container` only in v1.**
- gVisor: its GPU proxy supports NVIDIA only.
- microVM: an AMD GPU needs VFIO PCI passthrough into the VM, which this repo does not do.
- confidential: there is no AMD GPU TEE path in this repo.

The executor refuses these with `IsolationUnavailableError` (no silent downgrade). The
scheduler applies the same rule from one helper, so it never places such a job on an AMD
machine. CPU-only jobs on AMD machines keep whatever tiers the machine supports.

**D6. Device passthrough is minimal.** An AMD GPU job gets:
- `--device /dev/kfd`;
- `--device /dev/dri/renderD<N>` for **only** the GPUs selected for the job;
- `--group-add <numeric GID of the host "render" group>` and the same for `video`.

Forbidden: `--privileged`, `--ipc=host`, `--security-opt seccomp=unconfined`, mounting
all of `/dev/dri`, and `--cap-add`. Every existing ADR-0003 hardening flag stays. If
PyTorch needs shared memory, use a bounded `--shm-size`.

**D7. Verification never penalises a vendor difference.**
- Shadow re-runs only go to a machine with the **same `gpu_vendor`** as the primary,
  preferring the same `gpu_model`. If none exists, return no shadow (the check is
  inconclusive, as it is today when no independent machine exists).
- Known-answer challenges store expected hashes **per `gpu_runtime`**. If there is no
  expected hash for the runtime that ran the job, the result is **inconclusive**. It
  never fails, and it never touches reputation.
- A real mismatch within the same vendor is still caught exactly as today.

**D8. Backfill and old agents.**
- Existing machines with `gpu_count > 0` are backfilled as `nvidia` / `cuda`.
- A report from an old agent that lists GPUs without a vendor is treated as
  `nvidia` / `cuda`, because old agents can only detect NVIDIA.
- Existing jobs are backfilled as `required_gpu_runtime = 'cuda'`.

**D9. Honesty is unchanged.**
- The agent reports AMD GPUs as `DETECTED`, never `VERIFIED` or `BENCHMARKED`.
- A metric that a vendor does not expose is `None`, never estimated or fabricated.
- Simulated output is never stamped `hardware`.

**D10. No money code changes.** Pricing, holds, capture, settlement and the ledger are
vendor-neutral and must not be edited for this work.

---

## 3. Master prompt

Copy everything inside the block into the coding agent. It is written to be run one work
package at a time, with a review between packages.

~~~text
You are implementing AMD GPU support (ROCm) in the ComputeCoin repository at the repo
root. NVIDIA support exists and must keep working exactly as it does today.

READ FIRST, BEFORE WRITING CODE
1. docs/operations/amd-gpu-support-plan.md. Section 2 holds LOCKED design decisions
   D1-D10. Follow them exactly. If one looks wrong, stop and ask; do not work around it.
   Section 4 is the checklist you will tick.
2. docs/operations/EXECUTION_PLAN_PHASES_A_D.md, "Global guardrails". They apply here.
3. docs/architecture/ADR-0003-isolation.md and docs/operations/gpu-validation-runbook.md.
4. The code you will change:
   - provider-agent/computecoin_agent/hardware.py: GPUInfo, CapabilityReport,
     detect_gpus, contract_capability_report.
   - provider-agent/computecoin_agent/executor.py: GPU argv block near "GPU only when
     requested", the isolation tier block, and metrics collection after the run.
   - provider-agent/computecoin_agent/telemetry.py: sample_gpu_metrics. Note its
     injectable nvml_module parameter; copy that pattern for amdsmi.
   - provider-agent/computecoin_agent/isolation.py
   - apps/api/app/models/provider.py (HardwareInventory, ProviderCapability),
     apps/api/app/models/jobs.py, apps/api/app/models/model_registry.py
   - apps/api/app/schemas/provider.py (GpuReport, HardwareOut), schemas/jobs.py,
     schemas/marketplace.py
   - apps/api/app/api/routers/agents.py: enrollment writes HardwareInventory from the
     report.
   - apps/api/app/services/scheduler.py: validate_job, _eligible_machines.
   - apps/api/app/services/marketplace.py: search filters.
   - apps/api/app/services/inference.py: routing and image choice.
   - apps/api/app/services/verification.py: is_deterministic, _pick_shadow_machine,
     shadow comparison.
   - apps/api/app/services/challenge.py: expected-hash comparison.
   - apps/api/alembic/versions/: the newest is 0017_token_deposits.py; yours is 0018.

HOW TO WORK
- Do the work packages in order, WP0 to WP8. After each one:
  (a) run the suites it touches;
  (b) tick its items in section 4 of the plan;
  (c) commit with a conventional message, e.g. "feat(agent): detect AMD GPUs via amdsmi";
  (d) report, then wait for review before starting the next package.
- Match the surrounding code: docstrings that explain WHY, honest notes, Decimal for
  money, type hints, and the same test layout and fixtures.
- Every behaviour change ships with a test that fails without the change.
- No AMD GPU is available in this environment. Mock amdsmi through an injectable module
  argument, the way telemetry.py injects nvml_module. Never require real hardware in the
  automated tests.
- The amdsmi Python API changes between ROCm releases. As of ROCm 6.x it has functions
  such as amdsmi_init, amdsmi_get_processor_handles, amdsmi_get_gpu_asic_info,
  amdsmi_get_gpu_vram_info, amdsmi_get_gpu_vram_usage, amdsmi_get_gpu_activity,
  amdsmi_get_temp_metric, amdsmi_get_power_info, amdsmi_get_gpu_device_uuid and
  amdsmi_shut_down. Verify every name against AMD's docs for the ROCm version you target
  before relying on it. Wrap every call so a failure gives "CPU-only" plus a note,
  never an exception. That is the same contract detect_gpus has today.

WORK PACKAGES

WP0 Baseline (no code changes)
- Run make agent-test, make api-test and make web-check, plus the SDK tests
  (sdk/python: pytest; sdk/typescript: npx vitest run and npx tsc --noEmit).
  Record the pass counts.
- Confirm whether the agent's jobrunner can run more than one GPU job at once on a
  machine. If it can, stop and report: AMD device selection then needs a per-job GPU
  allocator, which is a design change.
- Report which isolation tiers the NVIDIA path currently combines with --gpus. In
  particular, check whether "--runtime kata-qemu" plus "--gpus" really reaches the GPU.
  Record findings only; do not change NVIDIA behaviour.

WP1 Agent detection (hardware.py)
- Add gpu_vendor, gpu_runtime and gpu_runtime_version to GPUInfo. Add a private
  render-node field for AMD, used only by the executor and never sent to the server.
- Move the current NVML body into _detect_nvidia_gpus() unchanged. Add
  _detect_amd_gpus(amdsmi_module=None). detect_gpus() calls both and applies D2: one
  vendor; if both are found, use config gpu_vendor, otherwise return no GPUs with a
  clear note, and enrollment refuses.
- Read the ROCm version (amdsmi library version, or /opt/rocm/.info/version) and the
  CUDA driver version, both best-effort.
- contract_capability_report sends vendor, runtime and runtime_version for each GPU.
- Add an optional "amd" dependency extra. State in the README that amdsmi must match
  the host's ROCm version.
- Tests: AMD detected (mocked); amdsmi missing; amdsmi init failure; both vendors
  without config; both vendors with config; NVIDIA tests unchanged and passing.

WP2 Agent execution (executor.py, isolation.py)
- Add _amd_available(): /dev/kfd exists and Docker is present.
- One helper builds GPU argv for a vendor and gpu_count. NVIDIA output must be
  byte-identical to today. AMD output follows D6, using the first gpu_count AMD render
  nodes and numeric host GIDs for render and video.
- AMD GPU job with a tier other than container: raise IsolationUnavailableError (D5).
  AMD GPU job without /dev/kfd: raise HardwareUnavailableError.
- Collect run metrics from whichever vendor ran the job.
- Tests: exact argv for AMD with 1 and 2 GPUs; none of the D6 forbidden flags appear;
  every ADR-0003 hardening flag is still present; tier refusals; missing kfd;
  NVIDIA argv snapshot unchanged.

WP3 Agent telemetry (telemetry.py)
- sample_gpu_metrics gains amdsmi_module=None. Map GPU activity to gpu_util_pct, VRAM
  used to mem_used_mb, edge or hotspot temperature to temp_c, and socket power to
  power_w. A metric the device does not expose (fan speed on many Instinct cards) is
  None.
- The signed batch format is unchanged.
- Tests: mocked full readings; partial readings give None; failure falls back exactly
  as the NVML path does.

WP4 Backend schema and ingest
- Migration 0018:
  - hardware_inventory gets gpu_vendor, gpu_runtime, gpu_runtime_version;
  - jobs gets required_gpu_runtime (NOT NULL, server default 'cuda');
  - new table model_version_images;
  - per-runtime expected hashes for verification challenges (a JSON map or a child
    table: choose the smaller change and explain why).
  - Backfill per D4 and D8. Write a working downgrade.
- Update models, schemas (GpuReport, HardwareOut, job create/out, marketplace listing)
  and the agents.py ingest. Reject unknown vendor or runtime values, and mixed-vendor
  reports, with 422.
- Tests: migration upgrade, downgrade and upgrade again on a populated DB; backfill
  values; an old-agent report without vendor gives nvidia/cuda; mixed vendors give 422.

WP5 Scheduler, marketplace, inference
- One helper decides job/machine GPU compatibility:
  - runtime match ("any" matches both);
  - an image exists for the machine's runtime when the model version is hardware;
  - the D5 tier rule.
  Use it in validate_job, _eligible_machines and inference routing, and nowhere else.
  Log skip reasons the way "scheduler skip (gpus)" does today.
- Add gpu_vendor and gpu_runtime filters to marketplace search.
- Inference picks the image with image_for(mv, machine.gpu_runtime).
- Tests: cuda job never placed on AMD; rocm job never placed on NVIDIA; any job uses
  both; no ROCm image means the AMD machine is skipped; AMD with gvisor is skipped;
  inference sends the right digest per vendor; existing scheduler tests unchanged.

WP6 Verification (verification.py, challenge.py)
- _pick_shadow_machine keeps today's rules (different machine, different provider) and
  adds: same gpu_vendor as the primary; prefer the same gpu_model; otherwise the
  cheapest. If there is no candidate, return None.
- A shadow job is pinned to the primary's gpu_runtime even when the primary was "any".
- Challenges compare against the expected hash for the runtime that actually ran. If
  there is none, the result is inconclusive with a recorded reason, and reputation is
  untouched.
- Tests:
  - an honest AMD provider whose output differs from the NVIDIA bytes is never marked
    mismatched;
  - a cheating AMD provider is caught by an AMD shadow;
  - an AMD challenge with no ROCm hash is inconclusive;
  - all existing verification and reputation tests pass unchanged.

WP7 Frontend and SDKs
- Job submission form (apps/web/src/app/dashboard/jobs/new/page.tsx): a gpu_runtime
  select (CUDA / ROCm / Any) with one line of help text; default CUDA.
- Provider hardware page (apps/web/src/app/provider/hardware): vendor and runtime
  badge.
- Marketplace listing and filters: vendor badge and filter. Admin jobs and machines
  tables: vendor column.
- Python and TypeScript SDKs: optional gpu_runtime on job submission, the types, the
  README and tests.
- make web-check and make web-build pass. SDK tests pass.

WP8 Documentation
- docs/api/phase2-provider-protocol.md: GpuReport fields.
- docs/api/phase4-execution-protocol.md: required_gpu_runtime and image selection.
- ADR-0003 addendum: the D5 tier matrix and D6 device rules.
- docs/security/threat-model.md: AMD device passthrough, render-node scoping, GIDs.
- docs/operations/gpu-validation-runbook.md: an AMD host section (ROCm install,
  /dev/kfd and render nodes, agent detect, a test job).
- implementation-checklist.md: a new phase entry with honest test counts.
- PLATFORM_GUIDE.md: provider requirements for AMD.

GUARDRAILS
Do:
- Keep NVIDIA behaviour identical, and prove it with snapshot or argv tests.
- Keep every decision in one helper the scheduler, executor and inference all call.
- Keep detection failure-safe: a broken driver means CPU-only plus a note, never a
  crash.
Do not:
- Touch ledger, pricing, holds, settlement or contracts code (D10).
- Use --privileged, --ipc=host, seccomp=unconfined, --cap-add or a whole-/dev/dri mount.
- Mark anything VERIFIED from the agent, or invent a metric.
- Edit or delete an existing test to make it pass. If one must change, stop and explain
  why.
- Rename or drop existing columns. Add only.

STOP AND ASK THE HUMAN WHEN
- a locked decision in section 2 looks wrong;
- the amdsmi API you find differs from what WP1 or WP3 assume;
- any money, ledger or contract file would need to change;
- WP0 shows concurrent GPU jobs per machine;
- an existing test has to change;
- a step needs real AMD hardware (WP9 in the checklist is for a human).

REPORT AFTER EACH WORK PACKAGE
- Files changed, with one line each on why.
- Tests added, and suite pass counts before and after.
- Checklist items ticked, and any item not done, with the reason.
- Open questions and risks found.
~~~

---

## 4. Master checklist

Tick each item only when its proof exists: a passing test, a command output or a
reviewed document. "Proof" says what the reviewer checks.

### WP0 Baseline
- [x] All suites pass before any change; counts recorded (agent, API, web check, Python SDK, TypeScript SDK). *Proof: command output in the WP0 report.* → Agent: 99 passed; API: 606 passed; Web: 16 passed, typecheck & lint clean; SDK Python: 59 passed; SDK TypeScript: 20 passed, typecheck clean; Guardian: 19 passed; Witness: 7 passed; Contracts: 33 passed; e2e: 7 passed.
- [x] Jobrunner concurrency per machine confirmed (one GPU job at a time, or escalated). *Proof: code reference in the report.* → `computecoin_agent/jobrunner.py::run_loop` runs sequentially calling `run_one(...)`. At most one job is executed at a time per host. No concurrent GPU allocation needed.
- [x] Findings recorded on the current NVIDIA + microVM/gVisor + `--gpus` behaviour (no code changed). *Proof: report section.* → In `computecoin_agent/executor.py`, NVIDIA `--gpus` appends `--gpus <count>`. When tier is `microvm` or `gvisor`, it appends `--runtime kata-qemu` or `runsc`. For AMD, D5 strictly mandates `container` only, raising `IsolationUnavailableError` on any other tier.


### WP1 Agent detection
- [x] `GPUInfo` has `gpu_vendor`, `gpu_runtime`, `gpu_runtime_version`; the render node is private and not sent to the server.
- [x] `_detect_nvidia_gpus` is the old NVML body, behaviour unchanged. *Proof: existing hardware tests pass untouched.*
- [x] `_detect_amd_gpus(amdsmi_module=None)` returns GPUs with name, VRAM, UUID and `DETECTED` status.
- [x] Missing amdsmi, init failure and query failure each give CPU-only plus a note, never an exception.
- [x] Both vendors without `gpu_vendor` config: no GPUs offered, a clear note, and enroll refuses (D2).
- [x] The capability report sends vendor, runtime and runtime version for each GPU.
- [x] The optional `amd` extra and README note on matching the ROCm version are in place.
- [x] Tests cover every case above with mocked amdsmi.

### WP2 Agent execution
- [x] NVIDIA argv is byte-identical to before. *Proof: snapshot test.*
- [x] AMD argv has `/dev/kfd`, only the selected `renderD` nodes and numeric GIDs for render and video. *Proof: exact-argv tests for 1 and 2 GPUs.*
- [x] None of `--privileged`, `--ipc=host`, `seccomp=unconfined`, `--cap-add` or a whole `/dev/dri` mount appears. *Proof: a negative test.*
- [x] Every ADR-0003 hardening flag is still present for AMD jobs. *Proof: test.*
- [x] AMD GPU job + gVisor/microVM/confidential raises `IsolationUnavailableError`. *Proof: test.*
- [x] AMD GPU job without `/dev/kfd` raises `HardwareUnavailableError`. *Proof: test.*
- [x] Run metrics come from the vendor that ran the job, or are `None`.

### WP3 Agent telemetry
- [x] `sample_gpu_metrics(amdsmi_module=...)` maps utilization, VRAM used, temperature and power.
- [x] Metrics the device doesn't expose are `None`. *Proof: partial-reading test.*
- [x] The signed batch format is unchanged. *Proof: existing telemetry tests pass untouched.*

### WP4 Backend schema and ingest
- [x] Migration `0022` adds the columns and table; upgrade → downgrade → upgrade works on a populated DB. *Proof: test_migration_upgrade_downgrade_cycle passes.*
- [x] Backfill: machines with GPUs become `nvidia`/`cuda`; jobs become `required_gpu_runtime='cuda'`; existing `image_digest` values become `cuda` rows in `model_version_images`.
- [x] Per-runtime challenge hashes stored; the storage choice is explained in the migration docstring.
- [x] An old-agent report without a vendor ingests as `nvidia`/`cuda` (D8). *Proof: test_old_agent_report_without_vendor_defaults_to_nvidia_cuda passes.*
- [x] Unknown vendor or runtime → 422; mixed-vendor report → 422. *Proof: test_unknown_vendor_runtime_rejected_422 and test_mixed_vendor_report_rejected_422 pass.*
- [x] `HardwareOut`, job and marketplace schemas expose the new fields.

### WP5 Scheduler, marketplace, inference
- [x] One compatibility helper used by `validate_job`, `_eligible_machines` and inference. *Proof: grep shows only is_gpu_compatible in gpu_runtime.py and calls in validate_job, _eligible_machines, inference._find_eligible_machine.*
- [x] A `cuda` job is never placed on AMD; a `rocm` job is never placed on NVIDIA; an `any` job can use both. *Proof: test_jobs_only_go_to_machines_with_their_gpu_runtime passes.*
- [x] No image for the machine's runtime means the machine is skipped, with a logged reason. *Proof: test_scheduler_logs_clear_skip_reasons and test_inference_routing_amd_image_and_skip_logging pass.*
- [x] AMD + non-container tier is skipped by the scheduler (D5). *Proof: test_amd_machines_need_a_rocm_image_and_the_container_tier and test_scheduler_logs_clear_skip_reasons pass.*
- [x] Marketplace search filters by `gpu_vendor` and `gpu_runtime`. *Proof: test_marketplace_http_route_filters_by_vendor_and_runtime passes.*
- [x] Inference sends the right image digest for each vendor. *Proof: test_inference_routing_amd_image_and_skip_logging passes.*
- [x] Existing scheduler, marketplace and inference tests pass unchanged. *Proof: test_phase3_marketplace.py, test_phase4_jobs.py, test_phase6_inference.py pass.*

### WP6 Verification
- [x] Shadow machine: same vendor, different machine and provider, prefers the same GPU model. *Proof: test_shadow_machine_prefers_same_gpu_model in test_amd_scheduling.py passes.*
- [x] No same-vendor candidate means no shadow (inconclusive, as today). *Proof: test_shadow_job_pinned_to_primary_runtime in test_amd_scheduling.py passes.*
- [x] A shadow is pinned to the primary's runtime even for `any` jobs. *Proof: test_shadow_job_pinned_to_primary_runtime in test_amd_scheduling.py passes.*
- [x] An honest AMD provider with bytes that differ from NVIDIA is never mismatched or slashed. *Proof: test_honest_amd_provider_never_mismatched_or_slashed in test_amd_scheduling.py passes.*
- [x] A cheating AMD provider is caught by an AMD shadow. *Proof: test_cheating_amd_provider_is_caught_by_amd_shadow in test_amd_scheduling.py passes.*
- [x] Challenge with no hash for the runtime → inconclusive, reputation unchanged. *Proof: test_challenge_evaluates_against_runtime_hash_or_inconclusive in test_amd_scheduling.py passes.*
- [x] Existing verification, challenge and reputation tests pass unchanged. *Proof: test_b2_verification.py and test_b3_reputation.py (28 passed).*

### WP7 Frontend and SDKs
- [x] Job form has a CUDA / ROCm / Any select, defaulting to CUDA, with help text. *Proof: apps/web/src/app/dashboard/jobs/new/page.tsx updated with select and D5 container restriction; tsc and build pass.*
- [x] Provider hardware page shows a vendor and runtime badge. *Proof: apps/web/src/app/provider/hardware/page.tsx displays gpu_vendor and gpu_runtime badges.*
- [x] Marketplace shows a vendor badge and filter; admin tables show vendor. *Proof: LiveGpuTable in live-gpu-table.tsx has vendor/runtime filters and badges; AdminProvidersPage has vendor column.*
- [x] `make web-check` and `make web-build` pass. *Proof: tsc --noEmit, next lint (0 warnings), vitest (22 passed), and next build (all 53 pages) pass cleanly.*
- [x] Python and TypeScript SDKs accept optional `gpu_runtime`; types and READMEs updated; SDK tests pass. *Proof: Python SDK 76 passed; TS SDK 22 passed, typecheck clean.*

### WP8 Documentation
- [x] Phase 2 and Phase 4 protocol docs updated. *Proof: docs/api/phase2-provider-protocol.md and docs/api/phase4-execution-protocol.md.*
- [x] ADR-0003 addendum with the D5 tier matrix and D6 device rules. *Proof: docs/architecture/ADR-0003-isolation.md.*
- [x] Threat model covers AMD device passthrough. *Proof: Section 7 in docs/security/threat-model.md.*
- [x] GPU validation runbook has an AMD host section. *Proof: Section 8 in docs/operations/gpu-validation-runbook.md.*
- [x] `implementation-checklist.md` has a new phase entry with real test counts. *Proof: Phase 11 in docs/operations/implementation-checklist.md.*
- [x] `PLATFORM_GUIDE.md` lists AMD provider requirements. *Proof: Section 2.1 in docs/PLATFORM_GUIDE.md.*

### WP9 Real-hardware validation (a human runs this)
- [ ] Rent one AMD host (a Radeon RX 7900 or an Instinct MI300X) with a ROCm version supported by the chosen amdsmi; record the ROCm version.
- [ ] `computecoin-agent detect` shows the AMD GPU with the correct name, VRAM, vendor and runtime.
- [ ] Enroll; the admin console shows the machine as `amd`/`rocm`, `DETECTED`.
- [ ] Telemetry appears on the admin telemetry page, with `None` where the card has no sensor.
- [ ] A `rocm` job with a pinned ROCm image completes; the output is stored and billed on metered seconds.
- [ ] A `cuda` job is never offered to this machine.
- [ ] A gVisor or microVM job is refused, not downgraded.
- [ ] Inside the job container: only the assigned render node is visible, the process is not root, and there is no network.
- [ ] Results recorded in `gpu-validation-runbook.md`.

### Cross-cutting sign-off
- [ ] No file under ledger, pricing, billing, settlement or `contracts/` changed. *Proof: `git diff --stat` against the base.*
- [ ] No existing test was edited or deleted (or each one is explained and approved).
- [ ] Full suites green; counts compared with WP0.
- [ ] NVIDIA end-to-end path re-run (simulated locally, and on hardware if available) with no change in behaviour.
- [ ] `/security-review` or `/code-review` run on the full diff; findings fixed or recorded.
- [ ] The owner approves before merge.

---

## 5. Out of scope and follow-ups

- **Workload images.** The repo has no GPU workload Dockerfiles yet, for either vendor.
  Building pinned ROCm and CUDA images (vLLM and PyTorch both ship ROCm builds) is
  separate work. AMD providers earn nothing until those images exist and inference is
  real.
- **microVM for AMD** through VFIO passthrough, and **confidential computing on AMD
  GPUs**.
- **Machines with both vendors** offered at once (D2 limits v1 to one).
- **Per-job GPU allocator**, if WP0 finds concurrent GPU jobs per machine.
- **Same vendor, different GPU model.** Byte-exact re-runs between, say, an MI300X and a
  Radeon 7900 (or an A100 and an RTX 4090 today) may still differ. D7 prefers the same
  model; a tolerance-based comparison is a later improvement for both vendors.
- **Apple Silicon (inference only) and Intel**: separate plans.
