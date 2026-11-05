# U3 design note: Agent runtime v2

Status: code written, NOT run (no tests, builds or installs were executed). Awaiting owner approval (L8).
Implements ADR-0006 WP-U3 (checkpoints U3.1-U3.4). Builds on U1 (Job Spec v2, Kits), U2 (data plane), B5 (egress
allowlist, already in `executor.py`) and ADR-0003 (isolation, unchanged).
v1 jobs run exactly as before: every v2 code path is gated on the claim carrying a `v2` object.

## Owner decisions (NOT made here)

| Decision | Setting (agent config / env `COMPUTECOIN_*`) | Default (conservative) |
|---|---|---|
| Host cache size limit | `cache_max_bytes` | 20 GiB. `0` disables the cache (everything is re-downloaded) |
| Cache location | `cache_dir` | `~/.computecoin/cache` |
| Work area for staged inputs, checkpoints, outputs | `work_dir` | system temp dir |
| Checkpoint interval floor | `checkpoint_min_interval_seconds` | 60 (a Kit may ask for longer, never shorter) |
| Max bytes per checkpoint | `checkpoint_max_bytes` | 10 GiB |

The cache holds only Kit assets (models, weights). Customer inputs are staged into a per-job directory and
deleted when the job ends, unless the job plan marks an input `cacheable` (a public dataset). Reason: a shared
host cache must not keep one customer's private data after the job.

## Flow

1. **Claim.** The claim response gains one optional field `v2`, present only for a v2 job (non-private tiers
   only). It is the job's plan: `kit {slug, version}`, `inputs[{name, file_id, sha256, size_bytes, cacheable}]`,
   `assets[{name, file_id, owner_id, sha256, size_bytes, mount}]` (Kit models/assets, cached), `outputs[{name,
   path, required}]` (paths relative to `/out`, from the Kit, never the customer), `egress[]` (Kit-declared
   `host:port`), `checkpoint {dir, interval_seconds, resume}`. No URLs are in the claim: bytes never pass through
   the API and bearer URLs are issued just in time.
2. **Stage inputs (U3.1).** For each input and asset the agent asks `POST /v1/agents/v2/jobs/{id}/download-url`
   (machine-signed) and downloads straight from the store with the presigned URL. It hashes while streaming and
   compares size and SHA-256 with the plan; mismatch deletes the partial file and fails the job
   (`input_hash_mismatch`). Files are written `0444` in `0555` directories and bind-mounted into the container with
   `readonly` at `/in/<name>` (inputs) and `/cache/<name>` (assets). A write inside the container fails with
   EROFS: this is the U3.1 proof (argv test plus a real-docker test, the latter not run).
3. **Cache (U3.2).** Layout `<cache_dir>/<kit-slug>/<sha256[:2]>/<sha256>`; a Kit cannot see another Kit's
   directory (per-Kit). A hit is re-verified by size only (hash was proven when stored; a size mismatch evicts the
   entry) and `mtime` is touched (LRU clock). A miss downloads, verifies, then moves in atomically. Before a store,
   least-recently-used entries not pinned by a running job are evicted until the new object fits under
   `cache_max_bytes`; an object larger than the whole limit is used from the job dir and not cached. The cache
   counts `downloads` and `bytes_downloaded` (the "network counter" of U3.2).
4. **Run.** `DockerExecutor.build_argv` gains the read-only mounts, the checkpoint mount and environment
   (`COMPUTECOIN_INPUTS_DIR=/in`, `COMPUTECOIN_CACHE_DIR=/cache`, `COMPUTECOIN_CHECKPOINT_DIR=<kit dir>`,
   `COMPUTECOIN_RESUME=1` when resuming). With no v2 data the argv is byte-identical to before. All ADR-0003
   flags stay (non-root, read-only rootfs, tmpfs, no-new-privileges, cap-drop ALL, pids/memory/cpu, NVIDIA/AMD
   argv). Mount targets are validated: absolute, no `..`, not under `/proc /sys /dev /etc /out /tmp`.
5. **Egress (U3.3).** The Kit's `egress` list becomes `network_policy={"egress": [...]}` and goes through the
   existing B5 machinery unchanged (per-job `--internal` network, pinned proxy, allowlist, `DENIED` lines logged
   into the job log). Privacy tiers (sealed, sealed_pro, confidential) never get network: the plan is refused
   at claim time server-side and again in `build_argv`. No proxy image configured means the job is refused.
6. **Checkpoints (U3.4).** While the container runs, a thread every `interval` (>= floor) seals the checkpoint
   dir into a tar (stable member order, regular files only, no links), hashes it and uploads it through the
   data plane (create upload, part URLs, confirm, complete) as file `ckpt/<job_id>/<seq>.tar`. Only a completed
   (hash-proven) upload counts. To avoid a torn snapshot the Kit contract is: write files then atomically rename a
   `COMMIT` marker; the agent packs only when `COMMIT` exists and the dir was quiet for 2 s. On the next attempt,
   on any host, the claim `checkpoint.resume` names the newest proven checkpoint (highest `<seq>`); the agent
   downloads, verifies, safely extracts (no absolute or `..` members, no links, size caps) into the checkpoint
   dir and sets `COMPUTECOIN_RESUME=1`. A failed checkpoint upload is logged and never fails the job.
7. **Outputs.** After exit the agent collects each declared output path under `/out` (a file, or for a
   `dir` output every regular file; symlinks refused, size capped), hashes it and uploads it through the data
   plane as `out/<job_id>/<name>`. The result receipt lists `{name, sha256, size, file_id}` per output. A
   required output that is missing fails the job (`missing_output`). Bytes go straight to the store.

## Server (`app/api/routers/agent_v2.py`, prefix `/v1/agents/v2`, machine-signed like the other agent routes)

| Route | Purpose |
|---|---|
| `POST /jobs/{id}/download-url` `{kind: input|asset|checkpoint, name}` | presigned GET for one planned item |
| `POST /jobs/{id}/uploads` `{purpose: output|checkpoint, name, sha256, size_bytes, content_type, seq?}` | start (idempotent per job and name) |
| `POST /jobs/{id}/uploads/{uid}/parts/{n}/url` `{sha256, size_bytes}` | presigned PUT for one part |
| `POST /jobs/{id}/uploads/{uid}/parts/{n}/confirm` | check the stored part (409 resend if corrupt) |
| `POST /jobs/{id}/uploads/{uid}/complete` | assemble and prove the whole-file hash; returns `file_id` |

Authorisation: the job must be RUNNING on THIS machine, otherwise 404 (a host cannot touch another host's job,
and a job that failed over stops being writable by the old host). Items are looked up only in the job's own plan,
so the agent cannot ask for an arbitrary file id. Uploads are owned by the job's customer and their idempotency
key must start with `job:<job_id>:`. Everything is refused (fail closed) when `DATA_PLANE_ENABLED` is off.
The latest checkpoint is derived from `DataFile` rows named `ckpt/<job_id>/<seq>.tar`, so **no new table and no
migration** is needed.

Contract with U7: the plan is read from `job.data_policy["v2_plan"]` by one adapter,
`v2_plan_of(job)` in `agent_v2.py`. It is written at submission by `build_v2_plan` in `routers/jobs_v2.py` (also used by pipelines and fan-out), resolved from the Kit manifest and the spec.

## Security notes

- Presigned URLs are bearer secrets: never logged by the agent (only the host name is), never put in argv or
  container env.
- Hash verification happens on the host before mount; a tampered store object cannot reach a job.
- Tar extraction is hand-validated, not `extractall`.
- The container never sees the cache root, only the single objects mounted read-only.
- Egress and privacy: see 5. A private tier plan is dropped at claim and the agent refuses it too.

## Tests (`provider-agent/tests/test_u3_runtime_v2.py`, written, not run)

Argv: read-only mounts present, ADR-0003 flags still present, v1 argv unchanged, bad mount target refused,
privacy tier plus egress refused. Cache: hit does not download (counter), LRU eviction order, pinned entry kept,
oversize object not cached, size-mismatch entry evicted, per-Kit separation. Staging: hash mismatch refused.
Checkpoint: tar round trip, unsafe member refused, newest wins. Outputs: hash recorded, missing required output
fails. Still to do (needs real docker and a store): U3.1 write-fails-in-container, U3.3 live proxy block, U3.4 E2E-U8.

## Unverified

httpx streaming and `Range`-less download behaviour against a real presigned URL; the `--mount ...,readonly`
syntax (documented Docker flag, not executed); R2/S3 presign compatibility is U2's open item; the `COMMIT` marker
convention is new and must be written into the Kit authoring guide (U5).
