# U8: Bring your own container (BYOC)

Status: code written, NOT run or tested. ADR-0006 section 6, 11 (isolation) and WP-U8. Related: ADR-0003 (isolation),
U1 (Job Spec v2 / Kits), U3 (agent runtime v2), U7 (scheduler v2), U16 (abuse).

## Goal

A customer submits their own container image and runs it on a provider host. The image is untrusted code from a stranger
on a stranger's machine, so BYOC is ISOLATED BY DEFAULT and FAILS CLOSED everywhere: if anything needed for safety is
missing (allowlist, scanner, runtime, host support) the job is refused or stays unschedulable. It never falls back to a
weaker mode.

## Spec shape

`POST /v1/jobs/byoc` takes `{spec: {...}, project_id?, machine_id?, priority?}`. The spec is its own strict shape
(`app/schemas/byoc.py`, unknown fields refused). The Kit endpoint `POST /v1/jobs/v2` still refuses `image` / `command`
(`not_supported_yet`); a `mode: byoc` spec sent there gets the hint to use the BYOC route. `JobSpecV2` (Kit shape) is unchanged.

```json
{"version": 2, "mode": "byoc",
 "image": "ghcr.io/acme/tool@sha256:<64 hex>",
 "command": ["python", "/app/run.py"], "args": ["--n", "5"],
 "env": {"APP_MODE": "fast"},
 "isolation": "gvisor",
 "inputs": [{"name": "scene", "source": "dataset:<id>"}],
 "outputs": [{"name": "result", "path": "result/out.bin"}],
 "resources": {"gpu": {"count": 0}, "cpu": 2, "ram_gb": 4},
 "network": {"egress": []},
 "max_runtime_seconds": 3600,
 "billing": {"max_cost": "5.00"}}
```

Validation rules (all enforced in `app/services/byoc.py`):

* `image`: `registry/path@sha256:<64 hex>`. A tag, or no explicit registry host, is refused. The registry must match the
  `BYOC_REGISTRY_ALLOWLIST` setting (comma separated `host` or `host/org` prefixes). Empty allowlist = BYOC refused.
* `command` / `args`: at most 32 items each, 512 chars per item, 8192 total, no NUL. Passed as an argv list, never through a shell.
* `env`: at most 32 entries. Names must be on the allowlist (`TZ`, `LANG`, `LC_ALL`, `PYTHONUNBUFFERED`, `OMP_NUM_THREADS`,
  `NUMEXPR_NUM_THREADS`, `MKL_NUM_THREADS`, plus the `APP_` and `CUSTOM_` prefixes, plus `BYOC_ENV_ALLOWLIST`). Values at most
  1024 chars, no NUL or newline. Env is stored in the spec in plain text: it is NOT for secrets.
* There is no field at all for privileged mode, host mounts, devices, capabilities, host PID/net/IPC or the Docker socket.
  Unknown fields are refused, so these cannot be asked for. The agent never builds such flags for BYOC either (defence in depth).
* `isolation`: `gvisor` (default) or `microvm`. There is no `container`. Privacy tiers (sealed, confidential) are refused for BYOC.
* Resources are capped by settings (`BYOC_MAX_CPUS`, `BYOC_MAX_MEMORY_MB`, `BYOC_MAX_RUNTIME_SECONDS`, `BYOC_MAX_GPUS`).
  Billing is per GPU-second, so a BYOC job holds at least 1 GPU (default 1, capped by `BYOC_MAX_GPUS`); exactly that many are passed through, only NVIDIA (ROCm is container-only in v1, so
  AMD BYOC is refused by the existing agent rule), and never more than asked.
* `network.egress`: refused unless `BYOC_ALLOW_EGRESS=true`; then exact `host:port` entries (max 8) that ride the B5 egress
  proxy allowlist. Default is no network.

## Image scanning and denylist

`app/services/image_scan.py`:

* `ImageScanner` interface: `async scan(image_ref) -> ScanResult(status, scanner, findings)`; status is `clean`, `denied`,
  `unconfigured` or `error`. Only `clean` lets a job in.
* `UnconfiguredScanner` is the default (`BYOC_SCANNER=""`): returns `unconfigured`, so BYOC is refused with
  `byoc_scanner_unconfigured`.
* `TrivyScanner` (`BYOC_SCANNER=trivy`, `BYOC_TRIVY_BIN`, `BYOC_SCAN_TIMEOUT_SECONDS`): runs `trivy image --format json` with an
  argument list (no shell). Denies when CRITICAL vulnerabilities exceed `BYOC_SCAN_MAX_CRITICAL` (default 0) or a package /
  file name matches a known miner or malware tool list. Any scanner error is `error` = refused. HONEST LIMIT: Trivy is a
  vulnerability and secret scanner, not an antivirus. A miner compiled into a custom binary is not found by it. Real malware
  detection needs a second scanner plugged into the same interface plus the runtime monitoring in U16/U10 (unverified).
* Scan results are cached per digest in `byoc_image_scans` (`BYOC_SCAN_TTL_SECONDS`, default 24 h), so a re-submit does not rescan.
* Denylist table `byoc_image_denylist` (digest, reason, who, when). Admin routes: `GET/POST /v1/admin/byoc/denylist`,
  `DELETE /v1/admin/byoc/denylist/{digest}`, `GET /v1/admin/byoc/status`. Checked at submit (and again at VALIDATING) and at
  claim: a denylisted digest fails the claimed job (`byoc_image_denied`), refunds the hold and frees the machine, even if it
  was queued before the digest was added.

## Placement (fail closed)

The agent advertises isolation runtimes in the heartbeat: `health.isolation_runtimes` = subset of `["gvisor","microvm"]`
(`runsc` present, Kata/Firecracker present). The API sanitizes it (`HeartbeatHealth`). `scheduler_v2.machine_fit_reason`
returns `isolation_runtime` for a BYOC job on a machine that does not advertise the required runtime, so the job is never
placed there: it stays QUEUED (unschedulable) until such a machine exists. A targeted `machine_id` without the runtime is
refused at submit (409). The advertisement is self-reported; the real gate is the agent: it refuses to start the container
with the ordinary runtime (`IsolationUnavailableError`, job fails) rather than downgrade.

## Agent execution

`provider-agent/computecoin_agent/byoc.py` re-validates the `v2.byoc` block from the claim (digest, argv caps, env names,
limits) and returns a `ByocPlan`. `DockerExecutor.build_argv`, when the claim has a byoc block:

* forces tier `gvisor` (`--runtime runsc`) or `microvm` (`--runtime kata-qemu`); unavailable = refuse;
* read-only rootfs, `--cap-drop ALL`, `no-new-privileges`, non-root user, `--network none` unless declared egress, tight
  memory / cpu / pids / tmpfs from the byoc limits, no swap, `--ipc none`, digest-pinned image, the usual
  `/out` bind plus read-only `/in`;
* GPU count comes from the byoc block, never more than asked;
* appends `--entrypoint <command[0]>` and the argv after the image.

## Data

Migration `0035_byoc`: `byoc_image_denylist`, `byoc_image_scans`. Jobs use existing columns: `workload_type="byoc"`,
`spec_v2={"byoc": ..., "effective_resources": ..., "kit_image": <digest>}` (the claim already reads `kit_image` for the
image), `data_policy.v2_plan` (inputs/outputs/egress and the `byoc` block). `kit_id` stays NULL.

## Escape tests

`provider-agent/tests/escape/` holds one test per attack (rootfs write, mount, docker.sock, host PID and network namespaces,
`/proc/1/environ`, cloud metadata, device nodes, ptrace). Each runs a tiny probe inside a container built with the SAME argv
as production (`DockerExecutor.build_argv`) and asserts the attempt FAILED. Opt in with `COMPUTECOIN_ESCAPE_TESTS=1`; they need
docker and, for the default tier, runsc. Written, never run in this change. A passing report is the evidence for U8.3.

## Not done / owner decisions

* Registry allowlist, scanner choice and Trivy install on the API host (or a scan worker): owner.
* Runtime resource monitoring and miner kill (E2E-U10) is U16/U19 territory.
* Redundancy sampling for BYOC verification is not wired; BYOC jobs use the existing verification path unchanged.
* Billing, ledger, pricing unchanged; BYOC uses the same GPU-second billing and hold.
* Registry pull credentials for private images are not supported (public or provider-reachable registries only).
