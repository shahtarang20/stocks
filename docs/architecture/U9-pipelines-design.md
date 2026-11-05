# U9 Pipelines (DAG): design note

Status: code written, NOT run or verified. Parent: ADR-0006 (WP-U9). Checkpoints: U9.1 render -> upscale -> encode
as one pipeline with artifact hand-off; U9.2 a failure in step 2 re-runs from step 2, not from the start.

## What a pipeline is

A pipeline is a small directed acyclic graph. Each **step** is one Job Spec v2 Kit job (`kind: "job"`) or one
fan-out job (`kind: "fanout"`, U4). An **edge** maps a named output of one step (a data-plane file, U2) to a
named input of a later step. The platform starts a step when every edge into it has a ready file. Nothing new is
billed by the pipeline layer: every step is an ordinary job with its own hold, metering and settlement (existing
functions). The pipeline code never touches the ledger, pricing or billing.

## Spec (version 1)

```
{
  "version": 1,
  "name": "render-upscale-encode",            // 1..128 chars, optional
  "steps": [ {"id": "render", "kind": "job", "spec": { ...Job Spec v2... }}, ... ],
  "edges": [ {"from": "render.frames", "to": "upscale.images"} ]
}
```

Full example: `docs/examples/pipeline-render-upscale-encode.json` (render -> upscale -> encode, two edges). The first step
reads a customer file (`dataset:<id>`); the later steps get their inputs only from edges.

Rules (each refusal is a field error `{field, code, message}`, 422, nothing is created):

| Rule | Field | Code |
|---|---|---|
| unknown field anywhere | `steps[i].x`, `edges[j].x` | `unknown_field` |
| 1..`PIPELINE_MAX_STEPS` steps (default 20, hard ceiling 50); at most 200 edges | `steps` / `edges` | `too_many_steps` / `too_many_edges` |
| step id `^[a-z][a-z0-9_]{0,31}$`, unique | `steps[i].id` | `invalid_id` / `duplicate_id` |
| kind is `job` or `fanout` (fanout needs `parallelism`; job refuses it) | `steps[i].kind` | `invalid_kind` / `parallelism_required` |
| edge endpoints look like `<step>.<name>` and name a real step | `edges[j].from/to` | `invalid_endpoint` / `unknown_step` |
| no self edge, no cycle | `edges[j]` / `edges` | `self_edge` / `cycle` |
| `from` names a real output slot of the source Kit, and that output is selected (the step's `outputs` list is empty or names it) | `edges[j].from` | `unknown_output` / `output_not_selected` |
| `to` names a real input slot of the target Kit | `edges[j].to` | `unknown_input` |
| an input slot is fed by one edge only, and not also given in the step's own `inputs` | `edges[j].to` | `input_fed_twice` |
| slot compatibility: the target slot kind must be `dataset`; if both slots declare `content_type` (output) and `accepts` (input), the type must be accepted | `edges[j]` | `incompatible_slot` |
| an edge may not start at a fan-out step (see limits) | `edges[j].from` | `fanout_output_not_mappable` |
| every step's own spec passes the U1 validator (Kit exists, signature intact, params, required inputs counting edges, resources) | `steps[i].spec.<field>` | the U1 code |
| own `dataset:` inputs are the customer's live files | `steps[i].spec.inputs[k].source` | `input_not_found` |
| privacy tiers refused (U18 later) | `isolation_tier` | `privacy_tier_not_supported_for_pipelines` |

Validation of a step with edge-fed inputs substitutes a placeholder dataset id for the missing inputs (the U1
validator checks names and shape, not file existence); real ids are filled in when the step starts.

## Data model (migration 0034)

* `pipeline_runs`: id, customer_id, project_id, name, status, spec (the validated pipeline as given), edges,
  priority, isolation_tier, required_gpu_runtime, idempotency_key, restart_count, outputs (final files of sink
  steps), error_code, created_at, finished_at.
* `pipeline_steps`: id, run_id, step_key, kind, spec (raw step spec), needs (edges into this step), status,
  job_id (the current attempt's job; for a fan-out step the parent job), attempts, outputs (`{name: {file_id,
  sha256, size_bytes}}`), history (one row per past attempt), error_code, started_at, finished_at.
  Unique `(run_id, step_key)`.

No column is added to an existing table.

## State machines

Run: `pending -> running -> succeeded | failed | cancelled`; `failed -> running` by restart.
Step: `pending -> running -> succeeded | failed`; `pending -> skipped` (an ancestor failed, or the run stopped);
`pending|running -> cancelled` (customer cancel). A step moves `pending -> running` only when every needed
upstream step is `succeeded` and its output file is live; then one ordinary job is created in QUEUED (the existing
recovery sweep schedules and holds it, as for fan-out parts).

Failure policy (fail fast, keep paid work): when a step fails, no new step starts. Pending steps become `skipped`.
Steps already running are left to finish, because they are paid for and their outputs are reusable on restart.
When nothing is running, the run becomes `failed` with `error_code = step_failed:<id>`.

Success: every step `succeeded`; `run.outputs` holds the output files of sink steps (steps nobody reads from).

## Restart from the failed step (U9.2, no double billing)

`POST /v1/pipelines/{id}/restart` on a `failed` run:

1. Steps `failed`, `skipped` and `cancelled` go back to `pending` (attempt appended to `history`, `job_id` cleared).
2. `succeeded` steps are kept as they are. Their jobs are never re-created, so they are never billed again. The
   restarted step reads the recorded output file ids of its upstream steps.
3. If an upstream output file was deleted or expired meanwhile and a not-yet-succeeded step still needs it, that one
   upstream step is also reset (and re-billed, honestly: the customer deleted the data). It is reported in the response
   (`reset_for_missing_output`).
4. Refused: run not `failed` (409 `not_restartable`), step attempts exhausted (`PIPELINE_MAX_STEP_ATTEMPTS`, default 5,
   409), restart count above `PIPELINE_MAX_RESTARTS` (default 10, 409).

A restart never touches a step that is `running`: the failed run only becomes `failed` once nothing runs.

## Cancel

`POST /v1/pipelines/{id}/cancel`: every running single-job step is cancelled like `POST /v1/jobs/{id}/cancel`
(free capacity, CANCELLED, release the whole hold; fan-out steps use the U4 `cancel_group`), pending steps become
`cancelled`, the run `cancelled`. Succeeded steps keep their outputs. A cancelled run cannot be restarted.
Idempotent on a finished run.

## Worker sweep

`sweep_pipelines(sm)` runs in `worker.py` with its own timer (`PIPELINE_SWEEP_INTERVAL_SECONDS`, default 15) and its
own try/except, behind the same `data_plane_configured` gate as fan-out. One transaction and one try/except per run,
`FOR UPDATE SKIP LOCKED`, so a broken run never blocks the others or any other worker step. `advance_run` is also
called once at submit inside a savepoint so the first step starts without waiting for the sweep.

## API

* `POST /v1/pipelines` (Idempotency-Key honoured per customer) body `{spec, project_id?, priority?, isolation_tier?,
  required_gpu_runtime?}` -> 201 run view.
* `GET /v1/pipelines/{id}` -> run, steps, outputs.
* `POST /v1/pipelines/{id}/cancel`, `POST /v1/pipelines/{id}/restart`.

Limits per customer: `PIPELINE_MAX_ACTIVE_PER_USER` active runs (default 20), 429-style refusal `too_many_active_pipelines`
(HTTP 429).

## Known limits and decisions

* A fan-out step's merged output is a manifest of many part files, not one file, so it cannot feed an edge in this
  slice (`fanout_output_not_mappable`). A fan-out step can be a leaf. A later package can add a "gather" step.
* Step jobs bypass `spend_guard` exactly as fan-out children do (the hold is made by the recovery sweep).
  The per-step `billing.max_cost` is the cap; a customer-wide pipeline budget is not in this slice.
* Kit slot compatibility is limited to what manifests declare today (slot `kind`, optional `content_type` /
  `accepts`). Stronger typing needs a manifest change (U1).
* Privacy tiers are refused until U18.

## Tests (written, not run): `apps/api/tests/test_pipelines_u9.py`

Pure graph validation (cycle, caps, ids, edges), slot compatibility, state advance with fake step jobs
(success chain, failure skips downstream, restart reuses upstream with unchanged job ids and no new job for
succeeded steps, cancel), API refusals for privacy tiers.
