# U7 design note: Scheduler v2 and the v2 submit path

Status: draft for owner approval (L8). Implements ADR-0006 section 10 (first slice) and WP-U7 checkpoints U7.1 to U7.4,
plus the wiring U1 left out: a way to actually submit a validated v2 job.
Out of scope (later packages): data plane fetch of inputs (U2), the agent running a Kit image (U3), fan-out and gang
splitting (U4), spot/preemption, speculative execution, smoke-test-per-host, price units in money code (U15).

## Interfaces

| Item | Where |
|---|---|
| `POST /v1/jobs/v2` | new `app/api/routers/jobs_v2.py`; body `{spec, project_id?, machine_id?, priority?, isolation_tier?, required_gpu_runtime?}` |
| scheduler v2 logic | new `app/services/scheduler_v2.py` |
| hooks in v1 scheduler | `validate_job`, `_eligible_machines`, `schedule_job` in `services/scheduler.py`; each is a guarded `if job.spec_v2` branch, v1 jobs take the old path byte for byte |
| heartbeat field | `health.kit_caches` (optional list of `slug@version`) in `HeartbeatHealth` |
| migration | `0028_job_spec_v2` (revises `0027_data_plane`) |

## Submit path (`POST /v1/jobs/v2`)

1. Verified user, customer lock, idempotency key (same rules as v1), project access check (reuses v1 helper).
2. `validate_job_spec_v2` (U1). It re-verifies the Kit signature and hash on every use (`assert_kit_intact`).
   A `KitError` becomes the same 422 `{"detail": {"errors": [...]}}` body as `/v1/kits/validate-spec`.
3. Job row is built from the validated spec: `workload_type="kit"`, `model="<slug>@<version>"`, `input={}`,
   `gpu_count` = effective GPU count, `estimated_minutes` = ceil(expected_units x Kit `est_gpu_seconds_per_unit` x gpu_count / 60),
   at least 1 (unit hint only; billing stays GPU-seconds, P5), `max_cost` from `billing.max_cost`.
   New columns: `spec_v2` = `{spec, effective_resources, kit_manifest_sha256, kit_image}`, `kit_id` (slug), `kit_version`.
4. Then the v1 flow unchanged: CREATED to VALIDATING (`validate_job`) to QUEUED, `spend_guard`, `schedule_job`
   (quote, hold via `reserve_for_job`, reserve capacity), same failure transitions (402 `insufficient_credits`, 403 `spend_limit_exceeded`).
   No ledger, pricing or billing code is edited; only existing functions are called (L3).
5. Privacy tiers: v2 jobs may only use `container` or `open` in this slice. `sealed`, `sealed_pro`, `confidential` are refused with
   422 `privacy_tier_not_supported_for_kits` because those tiers need `encrypted_input` and `dk_shares` that the v2 body does not
   carry (fail closed, L1/P7). The spec's own `privacy` key is already refused by U1. The v1 tier gate
   (`_machine_supports_tier_and_policy`: banned, region policy, stake, value at risk) still runs for every v2 job.
6. Network: none (L10). The job's `network_policy` stays NULL.

`POST /v1/jobs` and `JobCreate` are untouched.

## Scheduler v2

Hard filters (all in addition to the existing ones, which still run in the same order):
- Existing gates unchanged: deregistered/unpriced, `throttled`, offline, tier and policy (banned, stake, value at risk,
  region), GPU runtime compatibility (`is_gpu_compatible`), operator policy, free slots.
- Workload-in-capability check is skipped for v2 jobs (a Kit is not a model workload; Kit support per host arrives with U3's smoke test).
  The model-image check (D4) is skipped for the same reason (the Kit image is pinned by digest in the signed manifest).
- `machine_fit_reason(job, machine)`:
  - `vram`: per-GPU VRAM (`HardwareInventory.vram_mb`, taken as per card) must be at least `min_vram_gb * 1024`. Unknown (0) VRAM fails when a minimum exists.
  - `gpus`: the one host must have at least `gpu_count` GPUs. A job is never split across hosts. If no host has that many it stays QUEUED and
    follows the existing retry/`no_provider` rule (it keeps waiting while retries remain).
  - `compute_capability`: needs `hardware.raw["compute_capability"]`; when the job requires one and the host does not report it, the host is refused.
  - `cpu`, `ram`, `disk`: host totals from the inventory must meet the effective minimums (0 or unknown fails).
- Targeted `machine_id` is validated with the same function at submit.

Soft ranking (`rerank`): existing weighted score unchanged, plus a cache-affinity bonus
`SCHEDULER_V2_CACHE_BONUS` (default 0.15) when the machine's `last_health.kit_caches` contains the job's `slug@version`.
Bonus is small by design: it breaks near-ties toward warm hosts but cannot override a large price or reliability gap, and a
cache claim never bypasses any hard filter. Result is recorded in `match_explanation.factors.kit_cache` and `match_explanation.scheduler = "v2"`.
Tie-break order unchanged (score, price, registration time).

## Machine cache report

Agent heartbeat may send `health.kit_caches: ["blender-render@4.2.0", ...]`. Schema validator keeps only strings matching
the Kit reference pattern, de-duplicates, caps at 64 entries and each at 96 chars; invalid entries are dropped (not a 400, so a
confused agent does not drop off the network). The value is stored in `last_health` as today. It is self-reported and unverified:
the only effect is a small ranking bonus, so lying gains a host at most preference, never access or money (the existing
verification and reputation machinery still applies to its results).

## Data model (migration 0028, nullable, no data rewrite)

`jobs.spec_v2` JSON NULL, `jobs.kit_id` String(64) NULL, `jobs.kit_version` String(32) NULL, index on `kit_id`. No foreign key (Kits are
immutable and addressed by slug and version; yanking must not cascade). v1 rows keep NULLs.

## Tests (`tests/test_scheduler_v2_u7.py`, written, not run)

U7.1 12 GB card refused for a 16 GB job, 24 GB accepted; U7.2 warm host preferred at equal price, cold host still chosen when it
is the only fit, a large price gap beats the bonus; U7.3 4-GPU job on a 4-GPU host works, never on a 2-GPU host (stays queued); cache report
sanitised and capped; v2 submit route: valid job reaches RESERVED, spec stored, invalid spec 422, privacy tier refused, v1 submit unchanged;
banned and throttled hosts still skipped for v2 jobs. U7.4 is the existing suite (hooks are no-ops when `spec_v2` is NULL).
Bug re-insertion list: remove the VRAM check; remove the GPU-count check; remove the cache bonus; remove the privacy-tier refusal;
let v2 skip `_machine_supports_tier_and_policy`.

## Unverified / dependencies

Whether `vram_mb` is per card or total on every agent (assumed per card, matches marketplace filter use). The agent side (U3, `agent_v2.py` + `runtime_v2.py`) exists: a v2 job is matched and held here and run only by a U3-capable agent.
Nothing was executed (no tests, no migration run).
