# Kit: comfyui-workflow-runner

Runs one customer ComfyUI workflow (API format JSON) headless inside the container against baked-in models and an allowlist of node classes, and collects the produced images. Licences: see kits/comfyui-workflow-runner/README.md.

Status: wave 2 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `workflow_run`. Verification: `perceptual`.

## Licences (record, owner to confirm before publish, L4)

- **ComfyUI is GPL-3.0.** The image redistributes it unmodified and runs it as a separate process; shipping the image means offering the corresponding source (pinned commit in the archive) and the GPL text. **OWNER/LEGAL DECISION** before publishing.
- Every custom node and every model carries its own licence; only nodes and models whose licence allows hosted commercial use may be put in the archive and `allowed_nodes.json`. Record each one here (name, revision, licence, URL).
- Customer workflows and outputs stay the customer's.

## Pins you must supply

`ASSET_URL` and `ASSET_SHA256` (ComfyUI source at a pinned commit, the allowed custom nodes, the models, and `allowed_nodes.json` (one tar; layout: comfyui/, models/, allowed_nodes.json)), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` and fail the build.
`requirements.txt` versions are NOT yet checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Notes

Input `workflow`: ComfyUI API-format JSON (size cap 2 MiB, at most 500 nodes). Before anything runs, every node's `class_type` must be in `allowed_nodes.json`, and no string input may contain `..` or start with `/` (no path escapes through loader nodes). The server listens on 127.0.0.1 only and the container has no network; custom node code that is not in the archive cannot be added by a workflow. Outputs: PNG/JPEG/WEBP files collected from the run's history into `/out/outputs`. No checkpoint: a workflow is one unit.

## Owner decisions

- Verification: Only meaningful when every sampler seed in the workflow is fixed; workflows with random seeds cannot be verified. Thresholds pending U5.3 data.
- Whether this Kit is offered at all, its price hints (`est_gpu_seconds_per_unit` is null until benchmarked, U5.3) and the GPU model list in `smoke/smoke.json`.
