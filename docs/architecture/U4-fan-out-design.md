# U4 design note: Fan-out planner and merge

Status: draft for owner approval (L8). Implements ADR-0006 section 7 (fan-out / merge), section 10 (speculative
execution), section 11 (reliability rows) and WP-U4 (checkpoints U4.1 to U4.3).
Builds on U1 (Job Spec v2, Kits), U2 (data plane), U7 (scheduler v2). Nothing in the ledger, pricing, billing or
the v1 job routes is changed (P10): every child job is held, scheduled, metered and settled by the existing code.

## Scope

In: `parallelism` in Job Spec v2 for three split shapes (range, items, bytes); Kit-declared split rule; a parent
job plus child "part" jobs; windowed spawning of parts; re-run of a failed part alone; straggler copy with
first-valid-result-wins; ordered, hash-checked merge into the parent's outputs; parent status derived from parts;
cancel of the parent cancels the parts; a worker sweep step.
Out (refused, never silently ignored): a merge Kit (ADR example `merge: {kit: ...}`), a byte-level concatenation
into one object (the data plane store has no compose operation yet), privacy tiers for fan-out (U18), pipelines
(U9), multi-GPU gang parts (a part needing N GPUs still runs on one host as today).

## Customer-facing spec

`POST /v1/jobs/v2/fanout` takes the same body as `POST /v1/jobs/v2`. `parallelism` is accepted ONLY on this
route; `POST /v1/jobs/v2` and `POST /v1/kits/validate-spec` keep answering `not_supported_yet` for it.

```yaml
parallelism:
  split: { kind: range, from: 1, to: 500, chunk: 25 }     # inclusive; or
  # split: { kind: items, chunk: 10 }                     # items come from the Kit's items param
  # split: { kind: bytes, input: scene, chunk_bytes: 268435456 }
  max_parallel: 20            # optional, 1..200, parts running at once (default FANOUT_DEFAULT_MAX_PARALLEL)
  straggler_multiple: 3.0     # optional, 1.5..20; omit for the platform default, 0 is not allowed
  max_part_attempts: 3        # optional, 1..5
```

Bounds (422 with a field path): at most 1000 parts (`FANOUT_MAX_PARTS`); `chunk >= 1`; `to >= from`; range span
at most 10^9; items list 1 to 100 000 entries; bytes input must be a `dataset:` input whose size comes from the
database (never from the client). `merge` is not a field: the only merge is "ordered". A `merge` key is refused.

## Kit-declared split rule

The signed manifest gains an optional key `split` (U1 allows it; old Kits without it cannot be fanned out):

```json
"split": { "range": {"param": "frames"}, "items": {"param": "prompts"}, "bytes": {"param": "byte_range"} }
```

Each entry says which shapes the Kit supports and which param carries the slice. The planner patches a child's
`params[<param>]` and nothing else:

| Shape | Customer supplies | Child gets |
|---|---|---|
| range | no value for the Kit's range param | `{"from": a, "to": b}` (inclusive) |
| items | the full list under the Kit's items param | the slice of that list |
| bytes | no value for the Kit's bytes param, plus the `input` name | `{"offset": o, "length": n}` |

If the customer already set a range or bytes param, the spec is refused (`param_reserved_for_split`). Every child
spec is then validated by the unchanged `validate_job_spec_v2` (signature, JSON Schema, resource minimums), so the
Kit's own `param_schema` must allow the patched param, otherwise the whole fan-out is refused at submit with the
per-field error. All children are validated at submit, nothing is created when one is invalid.

## Data model (migration `0029_fanout`, revises `0028_job_spec_v2`)

`models/fanout.py`; no column added to `jobs`.

| Table | Columns |
|---|---|
| `fanout_groups` | `id`, `parent_job_id` (unique FK jobs), `customer_id`, `status` (`running`, `merging`, `completed`, `failed`, `cancelled`), `part_count`, `split` (JSON, the customer's rule), `plan` (JSON: output names, max_parallel, straggler_multiple, max_part_attempts), `merge_result` (JSON), `merge_attempts`, `error_code`, `created_at`, `finished_at` |
| `fanout_parts` | `id`, `group_id`, `idx` (0-based, unique with group), `patch` (JSON params patch), `weight` (units, for cost and hold share), `status` (`pending`, `running`, `done`, `failed`, `cancelled`), `attempts` (primary or retry runs started), `winner_job_id`, `duration_seconds`, `final_cost`, `error_code` |
| `fanout_part_runs` | `id`, `part_id`, `job_id` (unique FK jobs), `kind` (`primary`, `retry`, `straggler`), `status` (`active`, `won`, `lost`, `failed`, `cancelled`), `created_at` |

A part is a slot; a run is one child job working on that slot. A re-run and a straggler copy are new runs, never a
changed job, so each run has its own hold, its own metering and its own invoice.

## Parent job

An ordinary `jobs` row (`workload_type = "kit_fanout"`, `spec_v2` = the customer's spec, no machine, no hold).
It is walked CREATED, VALIDATING, QUEUED, MATCHED, RESERVED, RUNNING inside the submit transaction with the normal
FSM, so every event is recorded. It never stays in QUEUED or RESERVED (those are the states the recovery sweeper
re-drives); in RUNNING with `lease_expires_at = NULL` the lease recovery skips it. Terminal mapping:

| Group | Parent |
|---|---|
| all parts done and merged | RUNNING, VERIFYING, COMPLETED (`output` = merge result, `final_cost` = sum of winning runs) |
| a part used all its attempts | FAILED (`error_code = fanout_part_failed`), the other runs are cancelled |
| merge failed `FANOUT_MERGE_MAX_ATTEMPTS` times | FAILED (`fanout_merge_failed`); part outputs are kept |
| cancelled | CANCELLED |

Cancel: `POST /v1/fanout/{parent_job_id}/cancel` cancels the parent and every active run. The v1 route
`POST /v1/jobs/{id}/cancel` on the parent also works: the sweep sees a CANCELLED or `cancel_requested` parent and
cancels the parts (at most one sweep interval later).

## Windowed spawning

All part rows are written at submit (cheap); child jobs are created lazily, at most `max_parallel` parts active at
once. Reason: the v1 scheduler fails a QUEUED job that finds no host after its retries, so 500 queued children on
20 hosts would fail with `no_provider`. A child is created QUEUED with `retry_policy.max_retries =
FANOUT_CHILD_MAX_RETRIES` (so a lost host is retried by the existing lease recovery on the same child) and is
scheduled by the existing recovery sweep, which holds funds through `reserve_for_job` (spend limits included).
A child that still ends FAILED, TIMED_OUT or CANCELLED (outside our control) counts as a failed run of its part; the
part then gets a new `retry` run, alone, until `max_part_attempts`.

## Billing (no code change; U4.2)

A hold, metering and invoice exist per child job exactly as for any job. A failed or cancelled job is never charged
and its hold is released in full by existing code. A part is therefore billed once, for the GPU-seconds of the run
that succeeded. A finished part is never re-run, so its cost cannot repeat. The cancel helper used for losers does
what `POST /v1/jobs/{id}/cancel` does: `release_job_reservation`, FSM to CANCELLED, `billing.release_hold`.
Parent `max_cost` is shared out per child by weight (rounded up to 0.01, minimum 0.01). Retry and straggler runs
hold their own amount at the same time as the primary, so peak held funds can exceed the parent `max_cost`;
duplicates are capped (below). Policy for the loser's "fair share" (ADR section 10) is NOT decided: this slice
releases the loser's hold in full. If both copies finish before the sweep cancels one, both are billed for their
actual seconds (that invoice already exists and is not undone). Owner decision (L8): keep or pay the loser a share.

## Straggler copy (U4.3)

Each sweep: when at least `FANOUT_STRAGGLER_MIN_DONE` (3) parts are done, `median` of their run durations
(`metrics.duration_seconds`, else `finished_at - started_at`). A part with exactly one active run, in RUNNING for
longer than `multiple * median` (default 3.0), gets a `straggler` run (one copy per part, at most
`max(1, 10% of parts)` copies live). First valid result wins: a COMPLETED run whose declared output files exist,
are not deleted, and whose stored size equals the recorded size. If two runs are COMPLETED, the earliest
`finished_at` wins. The loser is cancelled, its hold released.

## Merge (U4.1)

When every part is done, the group moves to `merging`. For each declared output name, in part order, the merge
collects the winning child's data-plane file `out/<child job id>/<name>`. Each one is re-checked: file owned by the
customer, not deleted or expired, object ready, `head_size == size_bytes`, and when the object is at most
`DATA_PLANE_INLINE_VERIFY_MAX_BYTES` the store's SHA-256 equals the recorded SHA-256 (hashed by the store client in bounded chunks inside the worker process). The result, stored at `jobs.output` and `fanout_groups.merge_result`:

```json
{"fanout": {"parts": 20, "merge": "ordered_manifest",
  "outputs": {"frames": [{"index": 0, "file_id": "...", "sha256": "...", "size_bytes": 1, "part": {"from": 1, "to": 25}}]},
  "manifest_sha256": "..."}}
```

`manifest_sha256` is the SHA-256 of the canonical JSON of `outputs`, so a later re-read can be compared. Downloads:
`GET /v1/fanout/{parent}/outputs/{name}/{index}/download` returns a presigned link for that one file through the
existing U2 download function. A merge failure (store down, a file missing) is retried each sweep from the stored part
outputs, never by re-running parts (ADR section 11). An output file that is gone for good fails the group after
`FANOUT_MERGE_MAX_ATTEMPTS` sweeps. Combining the files into one object (for example concatenated bytes or an
ffmpeg encode) needs a merge Kit or a store compose call; both are future work, refused now.

## Routes (`app/api/routers/fanout.py`)

| Route | Purpose |
|---|---|
| `POST /v1/jobs/v2/fanout` | submit; 201 with the parent view; `Idempotency-Key` supported |
| `GET /v1/fanout/{parent_job_id}` | group view: status, counters, per-part state and runs |
| `POST /v1/fanout/{parent_job_id}/cancel` | cancel parent and parts |
| `GET /v1/fanout/{parent_job_id}/outputs/{name}/{index}/download` | link for one merged part file |

Privacy tiers: `isolation_tier` other than `container` or `open` is refused with `privacy_tier_not_supported_for_fanout`.
Other users' parents answer 404.

## Worker

`worker.sweep_once` gets one more step, `sweep_fanouts(sm)`, with its own try/except and a timer
(`FANOUT_SWEEP_INTERVAL_SECONDS`, default 15), only when the data plane is configured. Each group is advanced in its
own transaction with `FOR UPDATE SKIP LOCKED` and its own try/except, so one broken group never blocks others.

## Config added (`core/config.py`, all with safe defaults)

`FANOUT_MAX_PARTS=1000`, `FANOUT_DEFAULT_MAX_PARALLEL=20`, `FANOUT_CHILD_MAX_RETRIES=3`, `FANOUT_MAX_PART_ATTEMPTS=3`,
`FANOUT_STRAGGLER_MULTIPLE=3.0` (0 disables globally), `FANOUT_STRAGGLER_MIN_DONE=3`, `FANOUT_MERGE_MAX_ATTEMPTS=10`,
`FANOUT_SWEEP_INTERVAL_SECONDS=15`.

## Tests (`tests/test_fanout_u4.py`, written, not run)

Pure planner: range, items, bytes splits and bounds (1001 parts refused, reserved param refused, chunk 0). Submit:
parent plus parts created, children lazily bounded by `max_parallel`, privacy tier refused, `parallelism` still
refused on `POST /v1/jobs/v2`. Sweep: failed part re-runs alone (other parts' jobs unchanged, no invoice for the failed
one: U4.2), straggler copy after 3x median, first result kept and loser cancelled with hold released (U4.3), merge
ordered with a hash mismatch refused, parent cancel cancels parts, parent status derived.
Bug re-insertion list: merge sorted by completion time instead of index; straggler winner not checked for outputs;
cancel loser without `release_hold`; fan-out accepted on `/v1/jobs/v2`.

## Unverified / owner decisions

Nothing here was run. Migration, ORM mapping, SQLite and Postgres behaviour of `FOR UPDATE SKIP LOCKED` (ignored on
SQLite), and the interplay with agent result posting (output file naming `out/<job>/<name>` is taken from
`agent_v2`) need a real run. Owner decisions: loser fair-share policy; whether duplicates may exceed the parent
`max_cost`; merge Kit and store compose (future).
