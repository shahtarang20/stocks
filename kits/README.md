# Workload Kits: authoring guide

A Kit is a signed, versioned bundle (ADR-0006 section 8, U1 design note). This directory holds the wave-1 Kits:
`ai-image`, `blender-render`, `ffmpeg-transcode`, `upscale`. Status: **code written, never built or run.**

## Waves 2 and 3 (U12): candidate list, subject to OWNER approval

Written to the same pattern, never built or run: wave 2 `whisper-transcribe` (speech to text), `llm-finetune-lora`
(multi-GPU capable through U13: one host, or several nodes when multi-node is enabled), `video-upscale-frames` (pipeline
helper between two `ffmpeg-transcode` steps), `comfyui-workflow-runner`, `batch-embeddings`; wave 3
`3d-gaussian-splat-train`. ADR-0006 section 8 names categories (AI video, LLM serving, notebooks, scientific, TTS) that
are NOT built here: LLM serving needs services (U11), notebooks need sessions, AI video, TTS and scientific kits need an
owner choice of model and licence. The list, each Kit's licence record, the verification method and thresholds are owner
decisions. Weights and extra code arrive as one hash-pinned archive (`ASSET_URL`, `ASSET_SHA256`) and are re-checked at start.
Verification: `whisper-transcribe` (`wer`), `batch-embeddings` (`cosine`) and `llm-finetune-lora` (`loss_curve`) use metric plug-ins with conservative thresholds marked OWNER DECISION (see each README); do not publish with redundancy
sampling on until the owner decides.

```
kits/<slug>/
  kit.json            the manifest that gets signed (U1 schema; only the keys listed in scripts/kit_ci.py are allowed)
  params.schema.json  the JSON Schema; MUST equal kit.json "param_schema" (scripts/kit_ci.py check enforces it)
  Dockerfile          digest-pinned base, non-root, hash-checked downloads, weights baked in
  entrypoint.py       implements the container contract through kits/_common/kitrt.py
  smoke/smoke.json    smoke spec (>= 3 GPU models) + fixture generators
  README.md           licence record and owner decisions
```

Build context is `kits/` (so `_common/` is reachable): `docker build -f kits/<slug>/Dockerfile kits --build-arg BASE_IMAGE=...`.

## The image digest field

`kit.json` commits `"image": "ghcr.io/computecoin/kit-<slug>@sha256:PENDING"`. That value **fails registry validation on
purpose** (`image_must_be_pinned_by_sha256_digest`). CI builds and pushes the image, then `scripts/kit_ci.py fill-digest`
writes a copy of the manifest with the real digest. Never type a digest by hand and never commit one.

## Container contract (agent runtime v2, U3)

| Item | Value |
|---|---|
| Params | `COMPUTECOIN_INPUT` (`/out/input.json`): `{"params": {...}}`; flat object also accepted. Validated again inside the container |
| Inputs | read-only files at `/in/<input name>` (`COMPUTECOIN_INPUTS_DIR`). One file per declared input |
| Assets | read-only `/cache` (`COMPUTECOIN_CACHE_DIR`). Wave-1 Kits bake weights into the image instead (the manifest has no asset field) |
| Outputs | under `/out` at the paths in `kit.json` `outputs` (a file, or a directory whose regular files are all collected). No symlinks |
| Result | `COMPUTECOIN_OUTPUT` (`/out/output.json`): `{"ok": true, ...}` or `{"ok": false, "error": {"code","message"}}`; non-zero exit on failure |
| Checkpoint | `COMPUTECOIN_CHECKPOINT_DIR`; `COMPUTECOIN_RESUME=1` when restoring |
| Runtime | `--network none`, read-only rootfs, non-root, caps dropped, `/tmp` tmpfs `noexec`, writable `/out` only |

Not decided anywhere yet and assumed by these entrypoints: the param payload shape in `input.json`, and that a directory output
path (for example `/out/frames`) is collected recursively. Confirm both against the agent code when U7 wires the plan (U3 note says
"a file, or for a `dir` output every regular file").

## COMMIT marker convention (checkpoints)

1. Write checkpoint files completely into the checkpoint directory (write `name.part`, then `os.replace` to `name`).
2. Then publish them by atomically replacing `COMMIT` with JSON `{"seq": n, "files": {"relative/name": "<sha256>"}}`
   (`kitrt.commit`). Write `COMMIT.tmp` and `os.replace` it; never edit `COMMIT` in place.
3. The agent snapshots the directory only when `COMMIT` exists and the directory was quiet for 2 s (U3 note).
4. On resume (`COMPUTECOIN_RESUME=1`) a Kit trusts **only** files named in `COMMIT` whose SHA-256 still matches
   (`kitrt.committed_files`). Anything else in the directory is ignored and redone. A file written after the last `COMMIT`
   may be torn and is never trusted.
5. Checkpoint at natural unit boundaries (a frame, an image). A Kit with no cheap boundary (ffmpeg single encode) does not
   checkpoint; the planner (U4) splits the work instead.

## Determinism rules

- Every random choice comes from a recorded seed in the params (derive per-item seeds as `seed + index`). Prefer CPU
  generators for noise so GPUs agree.
- No wall-clock, hostname or GPU-count dependent behaviour in output; strip metadata (timestamps, host paths).
- Pin everything: base image digest, library versions (with hashes), model revision and file hashes (verified at start).
- Fixed ordering of items and files; fixed tile sizes; no "auto" settings that depend on free VRAM.
- Accept that **bit-exactness across GPU models is not achievable** for most GPU work. Declare `verification.method`
  `exact` only if the Kit is bit-reproducible on all supported hosts (test it); otherwise a metric method: `perceptual`, `psnr_ssim`, `wer` (transcripts), `cosine` (embedding matrices) or `loss_curve` (final training loss).
- Fail loudly (`KitError(code)`) instead of silently falling back (e.g. a requested GPU device not present).
- Treat all inputs as hostile: size caps, safe archive extraction, format whitelists, protocol whitelists.

## Optional runtime sections of the manifest

All three are optional; absent means no network, no assets, no checkpoint. They are part of the signed manifest.

```json
"egress": ["huggingface.co:443"],
"assets": [{"name": "weights", "sha256": "<64 hex>", "size_bytes": 123456, "file_id": "<uuid>", "owner_id": "<uuid>", "cacheable": true}],
"checkpoint": {"dir": "/ckpt", "interval_seconds": 300}
```

- `egress`: exact `host:port` entries (no wildcards, no IP literals, no internal names), validated by the same B5 validator as
  `network_policy`. The agent adds them to the job's allowlist (never for privacy tiers). At most 16.
- `assets`: read-only hash-pinned files mounted at `/cache` (`COMPUTECOIN_CACHE_DIR`) and kept in the host cache. Each names exactly
  one of `file_id` + `owner_id` (a data-plane file; the API checks its SHA-256 and size against the pin when a job is submitted) or
  an `https` `url`. A bare `url` asset is accepted by the validator but jobs for that Kit are refused at submit
  (`asset_url_not_supported_yet`) until the agent can fetch plain URLs. At most 32.
- `checkpoint`: `dir` (absolute, not under `/out`, `/in`, `/cache`) is mounted writable; the agent uploads it every
  `interval_seconds` (raised to the host minimum) and restores the newest one on a retry (`COMPUTECOIN_RESUME=1`).

## Manifest `mode` (batch, service, session)

- `batch` (default for ordinary Kits): runs as a job via `POST /v1/jobs/v2`, fan-outs and pipelines.
- `service`: a long-running HTTP server for U11 services. Needs a `service` block (below) and starts ONLY through `POST /v1/services`; `POST /v1/jobs/v2` refuses it with `mode_not_batch`. Declares no outputs; replicas settle without output verification.
- `session`: an interactive session (U10). Needs the `session` block and starts ONLY through `POST /v1/sessions`.

```json
"service": {"port": 8000, "health_path": "/health", "max_concurrency": 8}
```

`service`: `port` 1-65535 and `health_path` (absolute path) required, `max_concurrency` 1-64 optional (a service may ask for less, never more). The service request must use the same `port` and `health_path`. Any other key is refused; the block is refused under any mode but `service`.

## Session block (interactive sessions, U10)

Optional; makes the Kit launchable as an interactive session (`POST /v1/sessions` with `kit_ref`). Part of the signed manifest.

```json
"session": {"mode": "tcp", "tcp_port": 8888, "command": ["jupyter", "lab", "--ip=127.0.0.1", "--port=8888", "--no-browser"], "idle_timeout_seconds": 1800}
```

- `mode`: `terminal` (agent `exec`s `command`, default `/bin/bash`, into a container the image keeps alive) or `tcp` (an app listening on loopback `tcp_port`; `command` is the container's main process; viewable in the browser through the HTTP proxy page).
- `tcp_port`: integer 1024-65535, required for `tcp`, refused for `terminal`. `command`: 1 to 32 strings. `idle_timeout_seconds`: 60-86400; the customer's own idle setting may only be shorter.
- Any other key is refused. Sessions get no network unless the Kit declares `egress` (customers can only narrow it); private tiers are refused.
- The app is served under `/dashboard/sessions/<id>/proxy/`; the container gets that prefix in `COMPUTECOIN_SESSION_BASE_PATH` (for Jupyter use `--ServerApp.base_url` from it in your entrypoint script, and disable token and XSRF cookie checks, because the channel is already end-to-end authenticated and cookies are held by the proxy page).
- A Kit with a `session` block must declare `"mode": "session"` (and a `session` block is refused under any other mode). Session Kits start ONLY through `POST /v1/sessions`; `POST /v1/jobs/v2` refuses them with the field error `mode_not_batch`.
- Design: `docs/architecture/U10-interactive-sessions-design.md` sections 9 and 10.

## Verification section of the manifest

```json
"verification": {"method": "perceptual", "sample_fraction": 0.1, "min_samples": 1, "thresholds": {}, "owner_decision": "..."}
```

`method` is the registry key in `apps/api/app/services/verify_plugins` (`exact`, `perceptual`, `psnr_ssim`, `wer`, `cosine`, `loss_curve`). `thresholds` overrides
that plug-in's defaults (unknown keys and out-of-range values are rejected as `invalid_thresholds`, which is inconclusive,
never a failure of the host). **Acceptance thresholds and sample fractions are an OWNER DECISION**; the values committed are
conservative defaults to be tuned with honest cross-GPU benchmark data (U5.3). The signed manifest fixes them per Kit version.

## Smoke test spec (U5.1: pass on at least 3 GPU models)

`smoke/smoke.json` fields: `kit`, `gpu_models_required` (>= 3 distinct models), `fixture`, `input` (the `input.json`),
`limits.max_seconds`, `checks[]`, `record[]`, and optionally `host_classes_required` and `steps[]` (below). Run on each model:

```
python kits/tools/smoke_runner.py --smoke kits/<slug>/smoke/smoke.json --image <repo>@sha256:<digest> \
    [--input-file scene=/path/fixture] --report report-<model>.json
```

The runner checks the generic contract (exit 0, `output.json` ok, outputs exist, PNGs not blank) with the same container flags
as the agent. The kit-specific checks in `checks[]` (cross-GPU perceptual agreement, resume, no network, negative controls) are
compared across the per-model reports. A Kit may be published only when every model's report says `passed` and the cross-GPU
check passes; the report files are the U5.1 evidence ("Kit CI report"). The GPU model list is a proposal; the owner decides
the fleet. Smoke runs need GPU hardware that this repository's hosted CI does not have: use self-hosted runners with labels
`gpu-3090`, `gpu-4090`, `gpu-a100` (or change the matrix in `.github/workflows/kits.yml`).

### CPU-only Kits and host classes

A Kit whose `resource_defaults.gpu_count` is `0` runs with no `--gpus` flag (the runner reads `kit.json` next to `smoke/`). Its smoke
spec names its hosts in `host_classes_required` instead of GPU models: at least 3 distinct labels that are `cpu` or start with
`cpu-` / `cpu_` / `cpu.` (for example `cpu-a`, `cpu-b`, `cpu-c`). For a CPU-only Kit only `cpu` labels count; for a GPU Kit only
GPU model names count (`gpu_models_required`), so neither can claim the other's coverage. The report's `gpu` field is
`cpu (<arch>, <n> cores)`, or the `--host-class` value when given. CI runs these on self-hosted runners labelled `cpu-a`, `cpu-b`,
`cpu-c`.

Platform side, a `gpu_count` 0 Kit job is **refused** (`422 cpu_only_kits_disabled`) unless the owner sets `CPU_ONLY_KITS_ENABLED=true`.
When enabled: the job takes one scheduler slot, runs only on machines whose heartbeat health says `cpu_capable: true` (agent setting
`cpu_only_kits_enabled`), the claim tells the agent `gpu_count` 0, and the quote prices that slot at the machine's asking rate times
`CPU_ONLY_RATE_FRACTION` (default 0.1, rounded to cents, never below 0.01). Billing functions are unchanged; settlement meters the
quote. The fraction and whether CPU hosts are paid at all are OWNER DECISIONS.

### Smoke `steps[]` (service and session images)

A serving or interactive image never exits, so the batch contract (exit 0, `output.json`) does not apply. When `steps` is present
the runner runs ONLY the steps, in order; every check must be true. Each step has a unique lowercase `id` and a `type`.

`type: "service"` (a U11 Service replica image). The runner creates an `--internal` docker network, starts the image with the
service agent's flags (`--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --user 65532:65532`, nothing
published), reads the container IP, waits for the health path, sends every request by container IP, then removes the container and
the network (also on failure).

| field | meaning |
| --- | --- |
| `port` | integer 1..65535, the port the Kit serves on |
| `health_path` | safe absolute path, default `/health`; polled with GET until 2xx |
| `start_timeout_seconds` | default 600; not above `limits.max_seconds` |
| `requests[]` | `id`, `method` (GET or POST), `path`, optional `body` (JSON value or string) and `content_type` (default `application/json`), `timeout_seconds` (default 60), `expect` |

`expect` predicates (at least one): `status` (int or list; default any 2xx), `body_contains`, `body_regex`, and
`json`: a list of `{"path": "choices.0.message.content", "non_empty": true}` / `{"path": ..., "equals": ...}` / `{"path": ..., "type": "string"}`
(dot separated, digits index a list). Checks are named `<step id>:health` and `<step id>:<request id>`.

`type: "session"` (a U10 tcp session Kit; `kit.json` must have `session.mode: "tcp"`). The runner starts the image like
`session_agent.build_session_argv` (no network, read-only, non-root, the Kit's `session.command` as the main process), then retries
`docker exec -i <container> nc 127.0.0.1 <tcp_port>` with `request` on stdin until the response satisfies `expect` or
`ready_timeout_seconds` (default 120) passes, then removes the container.

| field | meaning |
| --- | --- |
| `tcp_port` | optional; must equal `kit.json` `session.tcp_port` |
| `request` | the raw text sent over the socket (for HTTP include the CRLFs, HTTP/1.0 so the server closes) |
| `ready_timeout_seconds` | see above |
| `expect` | `contains` and/or `regex` against the response text. Check name: `<step id>:response` |

`scripts/kit_ci.py check` validates all of this offline (a Kit with a `session` block must have a `session` step).

## Publishing flow (CI, `.github/workflows/kits.yml`)

1. `check`: offline gate for every Kit (`scripts/kit_ci.py check`).
2. `build`: build each image, push to GHCR, capture the digest, `fill-digest` into `dist/<slug>.json` (artifact).
3. `smoke`: GPU runners run the smoke spec against the pushed digest (needs the self-hosted runners above); CPU-only Kits run
   on runners labelled `cpu-a`, `cpu-b`, `cpu-c`. Service and session Kits (`llm-serve`, `notebooks`) use `steps`.
4. `sign-publish` (manual dispatch with `publish=true`, protected environment `kit-signing`, runner label `kit-signer`):
   signs with `scripts/sign_kit.py` using the key at the path in repository variable `KIT_SIGNING_KEY_PATH` **on that runner's
   disk**, then posts to `POST /v1/admin/kits` with the `KIT_ADMIN_TOKEN` secret.

**Deviation from the U1 note to confirm (L8):** U1 says the signer must run "never in CI". This workflow follows the work
order instead (CI signs with an offline-provided key), confined to one approval-gated job on a dedicated runner that holds the key
file. The key is never in the repository, never in a secret, and never uploaded; `sign_kit.py` reads it from a path. If the owner
prefers the strict reading, download the `dist/*.json` artifacts, sign on the offline machine and use `kit_ci.py publish`.

## Adding a Kit

Copy a wave-1 directory, change `slug`/`version`, keep `param_schema` an object with `additionalProperties: false`, keep output paths
under `/out/`, write the smoke spec, record the licence in the README, run `python scripts/kit_ci.py check`, add the slug to the
matrix in `kits.yml`. A published version is immutable: a fix is a new `version`.
