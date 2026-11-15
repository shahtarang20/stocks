# Container rental: rent a GPU, run your own code (design note and contract)

Written 2026-11-15. Status: **being built**. Owner approved the scope on 2026-11-15 ("start now and complete it").
Locked decisions that still apply: L3 (ledger, billing and pricing code is **not changed**; this feature only calls the existing session
settlement), L7 (no key or secret in the repo), L8 (this note comes before the code), and the agent rule that OS-specific code lives only in
`provider-agent/computecoin_agent/platform/`.

## 1. What this is

A second way to use a host's GPU, next to Kits. A customer picks a GPU on the marketplace, picks a **ready template** (PyTorch + JupyterLab, or a
vision stack) or brings **their own image**, and gets Jupyter or a terminal on that GPU. They keep a **workspace** while the rental runs, can pull
**datasets in** from their platform storage and push **results out** to it, and are billed per GPU-second by the existing session billing.

It is built on the existing interactive sessions (U10): the same relay, end-to-end encryption, hold-and-capture billing, idle and maximum
duration limits, and egress proxy. Nothing about Kits changes.

Why it exists: Vast.ai, RunPod and Akash all let a renter run their own container. Kits alone cannot serve "I will train my own model".

## 2. The trade the owner accepted

An ordinary hardened container shares the host's kernel. A host that opts in accepts that a stranger's code runs in such a container on the host's
machine. Therefore:

* **Hosts opt in**, per machine, with an explicit agent setting. Default is off. A host that never opts in serves Kits and practice jobs only.
* **Customers accept a written warning** at rent time (stored as `terms_accepted_at`): non-sensitive data only, the host can see the files, the
  workspace is deleted when the rental ends, nothing is guaranteed to survive a host failure.
* The platform switch `CONTAINER_RENTAL_ENABLED` is **off by default**. The owner turns it on.
* Private tiers (sealed, sealed_pro, confidential) never apply to rentals. Rentals are `container` tier only.
* Not offered, by design: SSH, inbound ports, host networking, devices, privileged mode, Docker socket. The agent's existing refusals stay.

## 3. Non-goals (this version)

SSH or scp; private-registry images (the host's Docker can only pull public registries); persistent storage that survives the host;
multi-node; Windows hosts; changing billing; a custom-image malware scanner (hosts opted in instead; BYOC with its scanner is unchanged).

## 4. Settings (API, `app/core/config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `CONTAINER_RENTAL_ENABLED` | `False` | Platform switch for rentals (customer images and templates). |
| `RENTAL_ALLOWED_REGISTRIES` | `"docker.io,ghcr.io,nvcr.io,quay.io"` | Comma list. An image reference's registry is the first path part when it contains `.` or `:` (or is `localhost`), else `docker.io`. `localhost` is never allowed. |
| `RENTAL_EGRESS_ALLOWLIST` | package and model hosts, see code | Comma list of `host:port`. A rental may only reach hosts on this list (default: no network). |
| `RENTAL_WORKSPACE_MAX_GB` | `100` | Largest `workspace_gb` a request may ask for. |
| `RENTAL_MAX_INPUTS` | `16` | Most input files per rental. |
| `RENTAL_MAX_MEMORY_MB` | `262144` | Largest `memory_mb`. |
| `RENTAL_MAX_CPUS` | `64` | Largest `cpus`. |
| `RENTAL_EXPORT_WINDOW_SECONDS` | `1800` | How long after a rental stops its host may still upload exported results. |
| `RENTAL_MAX_EXPORT_FILES` | `2000` | Most files one rental may export. |

Template list: `apps/api/app/data/rental_templates.json` (section 9).

## 5. Capability flag

`container_rental` is appended to `KNOWN_FLAGS` (`services/capability_flags.py`) and to `WINDOWS_FORBIDDEN_FLAGS`. The agent reports it only when
the host set `allow_container_rental = true`, Docker is usable, and the OS is not Windows.

## 6. Database (migration `0044_container_rental`, new columns on `compute_sessions`)

| Column | Type | Meaning |
|---|---|---|
| `rental` | bool, default false | The session runs a customer image or a template (not a Kit). |
| `template` | varchar(64) null | Template slug when a template was used. |
| `requested_machine_id` | uuid null | The customer picked this machine; only it may claim the session. |
| `workspace_gb` | int, default 0 | Workspace quota granted (0 = none beyond the container). |
| `memory_mb` | int null | Container memory limit requested. |
| `cpus` | int null | Container CPU limit requested. |
| `inputs` | json null | `[{"file_id","path","size_bytes","sha256"}]`, validated at create. |
| `terms_accepted_at` | timestamptz null | When the customer accepted the rental warning. |
| `export_requested_at` | timestamptz null | Customer asked to save results now. |
| `export_state` | varchar(16) null | `requested`, `running`, `done` or `failed`. |
| `exports` | json null | `[{"name","size_bytes","sha256","file_id"}]` saved to the customer's storage. |

## 7. API contract

### 7.1 `POST /v1/sessions` (existing route, new optional fields)

New fields: `template` (slug, `^[a-z0-9][a-z0-9-]{0,62}$`), `machine_id` (uuid), `workspace_gb` (int, default 0), `memory_mb` (int, default
16384), `cpus` (int, default 4), `command` (list of strings, custom image only, at most 32 items, no NUL), `inputs`
(`[{"file_id": uuid, "path": str}]`, default `[]`), `accept_rental_terms` (bool, default false).

Rules, in this order (first failure wins; all are HTTP 422 unless noted):

1. Old behaviour is unchanged when `template` is absent and the request is a Kit session or the old custom-image path: the existing error codes
   (`give_exactly_one_of_kit_ref_or_image_digest`, `custom_images_not_enabled_for_sessions`, ...) stay as they are when
   `CONTAINER_RENTAL_ENABLED` is false.
2. `template` together with `kit_ref` or `image_digest`: `template_excludes_kit_ref_and_image_digest`.
3. A rental is a request with `template` or `image_digest` while `CONTAINER_RENTAL_ENABLED` is true. If it is false and `template` is given:
   `container_rental_not_enabled`.
4. Rental requires `accept_rental_terms == true` (`rental_terms_not_accepted`) and `isolation_tier == "container"`
   (`rental_requires_container_tier`).
5. `image_digest` must match the existing digest pattern and its registry must be on `RENTAL_ALLOWED_REGISTRIES` (`image_registry_not_allowed`).
6. `template` must exist in the template list **and have a digest** (`template_not_found`, HTTP 404). Its image, `mode`, `tcp_port`, `command`
   and default egress come from the template; request values for those are ignored; the request's egress may only **narrow** the template's list.
7. Egress: every entry (after the normal `validate_network_policy`) must be in `RENTAL_EGRESS_ALLOWLIST` (`egress_not_allowed`).
8. `machine_id` (optional): the machine must exist, not be banned, be `online` and available, and report the `container_rental` flag
   (`machine_not_available_for_rental`). Stored as `requested_machine_id`.
9. `workspace_gb` in `0..RENTAL_WORKSPACE_MAX_GB`; `memory_mb` in `1024..RENTAL_MAX_MEMORY_MB`; `cpus` in `1..RENTAL_MAX_CPUS`
   (`rental_limit_out_of_range`). Non-rental sessions must leave these at their defaults.
10. `inputs`: at most `RENTAL_MAX_INPUTS`; `path` is a clean relative POSIX path (no leading `/`, no `..`, no empty part, each part at most
    100 characters of `[A-Za-z0-9._+=@ -]`, total at most 240; not starting with `results/`); paths unique; each `file_id` must be a ready file
    owned by the caller (`input_file_not_found`, HTTP 404). `size_bytes` and `sha256` are copied from the file at create time.

Money: unchanged. The hold, capture and release are the existing `sess-hold`, `sess-capture`, `sess-release`.

### 7.2 Claim eligibility (`services/sessions.claim_next`)

* A rental session may only be claimed by a machine that reports `container_rental`.
* A session with `requested_machine_id` may only be claimed by that machine.
* **One GPU user at a time.** A machine with a job in a running state cannot claim a session, and a machine with a live session
  (`SESSION_LIVE`, `gpu_count > 0`) is not handed a new job (the job claim route refuses it). This closes the case where a job and a session
  both use the same GPU.
* The claim response adds: `rental`, `template`, `workspace_gb`, `memory_mb`, `cpus`, `inputs` (`[{"path","size_bytes","sha256"}]`, **no URLs**),
  `export_window_seconds`.

### 7.3 Customer routes

* `GET /v1/sessions` and `/{id}`: `SessionOut` gains `rental`, `template`, `workspace_gb`, `memory_mb`, `cpus`, `machine_id`,
  `requested_machine_id`, `inputs` (path and size only), `export_state`, `exports` (`name`, `size_bytes`, `file_id`).
* `POST /v1/sessions/{id}/export` (verified user, owner only): rental session in `active` state only (`session_not_exportable`, 409). Sets
  `export_requested_at = now` and `export_state = "requested"`. If one is already `requested` or `running`, returns it unchanged (200).
  Rate limit: one new request per 60 seconds (`export_too_soon`, 429).
* `GET /v1/rental/templates` (no login needed): `[{"slug","name","description","gpu_required","mode","default_egress","libraries"}]` for templates
  that have a digest. Never includes the digest.
* `GET /v1/marketplace/machines`: items gain `container_rental: bool`; new query filter `container_rental=true`. The detail route gains the same field.

### 7.4 Host-agent routes (machine HMAC, same auth as the existing session routes)

* `POST /v1/agent/sessions/{id}/inputs` -> `{"inputs":[{"path","url","size_bytes","sha256","expires_at"}]}`. Allowed while the session is
  `starting` or `active` and belongs to the calling machine. Presigned download URLs are minted for the **customer's** files via
  `data_plane.create_download_link`; calling again refreshes them. Only files listed in the session's `inputs` can be fetched.
* `POST /v1/agent/sessions/{id}/export-start` -> `{"state":"running"}`; allowed while `export_state` is `requested` or `running`, or at session end
  (rental sessions only). Idempotent.
* `POST /v1/agent/sessions/{id}/uploads`, `/uploads/{upload_id}/parts/{n}/url`, `/parts/{n}/confirm`, `/complete`: the same bodies and replies
  as the job routes in `agent_v2.py` (`purpose` is implied `output`), owner = the session's customer, file name `session/<session id>/<name>`
  (internal name), idempotency key `session:<session id>:out:<name>:<sha256>`. `name` is a clean relative path (same rules as `inputs.path`,
  without the `results/` restriction). Allowed while the session is `starting`, `active`, or stopped **within `RENTAL_EXPORT_WINDOW_SECONDS`**,
  rental sessions only, the calling machine only. At most `RENTAL_MAX_EXPORT_FILES` files per session (`too_many_export_files`, 409).
* `POST /v1/agent/sessions/{id}/export-result` body `{"files":[{"name","size_bytes","sha256","file_id"}],"failed":bool,"error":str|null}` ->
  stores `exports` (replacing earlier entries of the same name), sets `export_state` to `done` or `failed`, clears `export_requested_at`.
  Each `file_id` must be a file this session created (owned by the customer, name starting `session/<id>/`).
* The existing `POST /v1/agent/sessions/{id}/heartbeat` reply gains `"export": true` while `export_state == "requested"`.

## 8. Agent contract

### 8.1 Settings (`config.py`)

`allow_container_rental: bool = False` (the opt-in), `serve_sessions: bool = True` (the installed agent also serves interactive sessions; Kit
sessions are safe, rentals still need the opt-in), `rental_root: Optional[str] = None` (default `<work_dir or ~/.computecoin>/rental`),
`rental_max_workspace_gb: int = 50` (the host's own cap, applied over the request), `rental_max_memory_gb: int = 0` (0 = 90 percent of the
host's RAM), `rental_max_cpus: int = 0` (0 = all but one core).

### 8.2 Capability flag and serving

`capabilities.py` reports `container_rental` as in section 5. `cli.start` runs the session loop in a background thread when `serve_sessions` is
true, Docker is usable and the platform is not Windows. A rental claim on a host that has not opted in is refused: the agent ends it as failed
without starting anything (defence in depth; the API already refuses).

### 8.3 Running a rental

1. **Workspace.** `<rental_root>/<session id>/workspace`, created private to the agent and made writable by the container user. It is bind-mounted
   read-write at `/workspace` with `nosuid,nodev`. Container environment: `HOME=/workspace`, `TMPDIR=/workspace/.tmp`,
   `COMPUTECOIN_WORKSPACE=/workspace`, `COMPUTECOIN_RESULTS=/workspace/results`. The directory is empty-or-staged at start and deleted at the end.
2. **Inputs (before the container starts).** Call `/inputs`, then `runtime_v2.download_verified` each file to `/workspace/<path>`. The path is
   re-validated on the host (no `..`, absolute, symlink or escape; parents created inside the workspace only). Time spent here is never billed
   (billing starts at `started`). Any failure: end the session as failed with nothing started.
3. **Container.** The executor's hardened argv plus: `--memory`, `--cpus` from the (host-capped) request, `--shm-size` (`min(2g, memory/4)`),
   the workspace mount, the environment above. Still `--read-only` root, caps dropped, non-root, no-new-privileges, pids limit, **no network**
   unless the request's egress list (already limited to the allowlist) puts it on the proxy. Refuse any argv containing `--privileged`,
   `--network=host`, `-p`, `--publish`, `--device`, `--cap-add`, `--pid`, `--ipc=host`.
4. **Quota.** Every 30 seconds measure the workspace size (`du`-style walk that does not follow links). Above the granted size (capped by
   `rental_max_workspace_gb`) the agent stops the rental (reason `workspace_full`), exports what is in `results/`, and ends the session.
5. **Export.** On the heartbeat's `"export": true` and again at the end of every rental, upload every regular file under `/workspace/results`
   (no links, no special files, at most `RENTAL_MAX_EXPORT_FILES`, each at most 20 GiB) with `runtime_v2.upload_file`-style multipart uploads
   through the session upload routes, then call `/export-result`. A failed upload is reported as `failed`; it never blocks ending the session.
   After the end-of-rental export (or the export window), the workspace is deleted.
6. **Abuse.** While the session runs, look at the container's process names about every 30 seconds (`docker top`); a name on the miner list
   (xmrig, ethminer, t-rex, phoenixminer, nbminer, lolminer, gminer, cpuminer, minerd, cgminer, bfgminer, kawpowminer, teamredminer,
   srbminer) stops the container and ends the session as failed. Process names are never sent anywhere.
7. **Ending.** Stop and remove the container, tear down the egress proxy, export, delete the workspace, call `/ended`. Always, including on
   errors (the existing `finally`).

## 9. Templates

`rental/templates/<slug>/Dockerfile` (+ requirements, entrypoint). Two templates:

* `pytorch-jupyter`: CUDA PyTorch, JupyterLab, common training libraries, a `ccdata` note in the README.
* `vision-jupyter`: the same plus OpenCV, albumentations, onnx, onnxruntime-gpu and facenet-pytorch (insightface is not included: it needs a compiler to build), pinned.

Both run Jupyter as a `tcp` session (port 8888, token and password disabled: the only way in is the encrypted channel), `ServerApp.root_dir`
= `/workspace`, no browser, and start in `/workspace`. **They use the image's own ENTRYPOINT** (`rental/templates/_common/entrypoint.py`, which points every
writable path into `/workspace` and gives Jupyter the browser base path from `COMPUTECOIN_SESSION_BASE_PATH`), so a template's `command` is
`null` in the JSON and the claim carries no command override. Do not put a Jupyter command in the JSON: a command override would skip the entrypoint. `apps/api/app/data/rental_templates.json` lists
`{slug, name, description, gpu_required, mode, tcp_port, command, default_egress, libraries, digest}`; `digest` is `null` until the build
workflow has produced and pinned it, and a template without a digest is **hidden and refused**. Build: `.github/workflows/rental-templates.yml`
(manual run, pushes to ghcr.io, prints the digests); `scripts/rental_fill_digests.py` writes them into the JSON.

## 10. Web contract

* New page `/dashboard/rent`: pick a machine (pre-filled from the marketplace), pick a template or paste an image digest, GPU count, memory,
  workspace size, maximum price, maximum duration, idle timeout, optional input files from the customer's storage, optional extra egress
  from the allowlist, and the **warning with a required checkbox**. Submits `POST /v1/sessions` with a browser-generated key pair (reuse
  `lib/sessions.ts`).
* Marketplace machine cards and detail show a "Container rental" badge and a **Rent this GPU** link to `/dashboard/rent?machine=<id>`.
* `/dashboard/sessions/[id]`: shows template, workspace size, inputs, the export state, the saved files with download links, and a
  **Save results now** button (calls `POST /v1/sessions/{id}/export`).
* `/provider/hardware`: shows "Container rental: on / off" from the machine's flags, with plain instructions to turn it on.
* All new text is plain language (no jargon), accessible, and covered by tests and the accessibility test list.

## 11. Failure modes and what happens

| Situation | Result |
|---|---|
| Host loses power or the agent dies mid-rental | The existing host-lost rule stops billing at the last heartbeat. The workspace is gone; the customer keeps only what was already exported. |
| Input download fails or a hash is wrong | The rental ends as failed before the container starts; nothing is billed. |
| Disk quota exceeded | The rental stops (`workspace_full`), results are exported, the customer is told. |
| Export upload fails | `export_state = failed`; the rental still ends; the customer can retry only while the rental is active. |
| Customer closes the browser | The idle timeout stops the rental. Training the customer left running dies with it unless they used a long idle timeout. |
| A job claim races a session claim on one machine | The server-side exclusion in section 7.2 refuses the second one. |

## 12. Test plan

Unit and route tests for every rule in section 7; agent tests for the argv refusals, workspace safety, input path validation, quota, export,
abuse stop and the host opt-in; a contract test that runs the real agent code against the real API app (test client) for create, claim, inputs,
export and settlement; web tests for the rent form, the warning gate, the session page and the host badge; the existing suites stay green.
Nothing here has been run on a real GPU; the first real run is the host owner's job and is listed in the runbook.

## 13. Addendum (2026-11-15): changes made after the first build

These follow from review and from looking for what would fail on the first real run. They change the contract above in these ways only.

1. **Automatic save.** While a rental runs, the agent saves changed files from `/workspace/results` every `AUTOSAVE_SECONDS` (300) through the same
   export routes, without a customer request. A file whose size and modified time are unchanged since it was last saved is not sent again, and an
   automatic save with nothing changed makes no API call. The manual and final exports use the same skip rule (the API merges, so the customer's list
   stays complete). The container's files are unlocked only under `/workspace/results` for an export, not the whole workspace.
2. **Resumable input downloads.** `runtime_v2.download_verified` continues a dropped download with a Range request (steps of 1 MiB), falls back to a
   full restart when the server ignores the range, never trusts a `.part` file from an earlier run, and still checks size and SHA-256 at the end.
3. **Network-filter gating.** A session whose `network_policy.egress` is non-empty is claimed only by a host reporting `egress_proxy`. A rental
   aimed at a machine without it is refused at create with `machine_cannot_filter_network` (422).
4. **Relay guard.** If `WEB_CONCURRENCY` (exported by `apps/api/scripts/entrypoint.sh`) is above 1 and `SESSIONS_RELAY_BACKEND` is not `redis`, a new
   rental is refused with `session_relay_not_ready` (503), and the API logs an error at start when rental is enabled.
5. **Miner net.** `docker top` is asked for `comm,args`; a stratum pool address on a command line counts as a miner as well as the name list.
6. **`computecoin-agent rental check [--image X]`.** A preflight (Docker, GPU, disk, network filter, a locked-down test container with the GPU and a
   workspace write). Exit code 1 on a failure. The agent version is 0.2.0, the first with `rental`; the host page shows older agents as "Update agent".
7. **Web.** With no `NEXT_PUBLIC_SESSION_PROXY_ORIGIN` the Rent page hides the templates and the web-page option and offers a terminal on the
   customer's own image only.
