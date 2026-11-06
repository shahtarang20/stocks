# Operator cheat sheet: one entry point per task, bash and PowerShell side by side

_Status: written 2026-11-06 for the cross-platform plan (W10, [plan](cross-platform-windows-linux-master-plan.md)). **Not run on Windows yet**:
every PowerShell command below is written but unexecuted until the Windows CI jobs and a real Windows machine have run it
(see [windows-developer-setup.md](windows-developer-setup.md) and `tests/windows/`)._

Rules that hold for every row:

* One command per task. The PowerShell wrappers in `scripts/windows/` only find Python and forward the arguments to the same
  Python script the bash column calls, so the two columns cannot drift apart.
* **No secret on a command line.** Tokens and keys come from an environment variable or a file path you name (L7). Do not paste a
  token into a script, a chat or a ticket.
* Run PowerShell commands from the repository root. They work in Windows PowerShell 5.1 and PowerShell 7. If scripts are blocked:
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` (this window only).
* Python is `python3` on Linux and `py -3` (or `python`) on Windows. The wrappers set `PYTHONUTF8=1` so the code page does not matter.

## Developer tasks

| Task | bash (Linux, macOS, WSL2) | PowerShell (Windows) |
|---|---|---|
| List the targets | `make help` | `py -3 tasks.py help` |
| Install the provider agent | `make agent-install` | `py -3 tasks.py agent-install` |
| Test the provider agent | `make agent-test` | `py -3 tasks.py agent-test` |
| Test the Python SDK | `cd sdk/python && pytest -q` | `py -3 tasks.py sdk-python-test` |
| Test the TypeScript SDK | `cd sdk/typescript && npm test` | `py -3 tasks.py sdk-ts-test` |
| Kit offline gate (manifests, schemas, Dockerfiles) | `python3 scripts/kit_ci.py check --kits-dir kits` | `py -3 tasks.py kits-check` |
| Kit unit tests | `pytest -q kits/tests` | `py -3 tasks.py kits-test` |
| Everything the Windows CI job runs | n/a | `py -3 tasks.py windows-test` |
| Web: install, dev server, build, typecheck | `make web-install` / `web-dev` / `web-build` / `web-check` | `py -3 tasks.py web-install` / `web-dev` / `web-build` / `web-check` |
| Start the whole stack in containers | `make up` (`make down`, `make logs`, `make ps`) | `py -3 tasks.py up` (`down`, `logs`, `ps`) |
| API: install, migrate, run, test | `make api-install` / `api-migrate` / `api-run` / `api-test` | Linux only (X3). Use WSL2 or Docker: `py -3 tasks.py --wsl api-test`, see [windows-developer-setup.md](windows-developer-setup.md) |
| Show what a task would run, run nothing | `make -n agent-test` | `py -3 tasks.py --dry-run agent-test` |
| Pass options to pytest | `make agent-test` then `pytest -k name` | `py -3 tasks.py agent-test -- -k name` |

Every Makefile target exists in `tasks.py` under the same name; `tests/windows/test_operator_tooling.py` fails when one is missing.
PowerShell shortcut for the same thing: `.\scripts\windows\tasks.ps1 agent-test`.

## Kits (admin)

| Task | bash | PowerShell |
|---|---|---|
| Offline gate for every Kit | `python3 scripts/kit_ci.py check --kits-dir kits` | `.\scripts\windows\kit_ci.ps1 check` |
| Put a pushed image digest into a **copy** of a manifest | `python3 scripts/kit_ci.py fill-digest --kit kits/ai-image/kit.json --repo ghcr.io/org/kit-ai-image --digest sha256:<64 hex> --out dist/ai-image.json` | `.\scripts\windows\kit_ci.ps1 fill-digest --kit kits\ai-image\kit.json --repo ghcr.io/org/kit-ai-image --digest sha256:<64 hex> --out dist\ai-image.json` |
| Create the offline signing key (once, on the signer machine) | `python3 scripts/sign_kit.py --gen-key /path/to/kit_signing.pem` | `.\scripts\windows\new_kit_signing_key.ps1 -Path C:\keys\kit_signing.pem` |
| Sign a manifest | `python3 scripts/sign_kit.py --manifest dist/ai-image.json --key /path/to/kit_signing.pem --out dist/ai-image.signed.json` | `.\scripts\windows\sign_kit.ps1 -Manifest dist\ai-image.json -KeyPath C:\keys\kit_signing.pem -Out dist\ai-image.signed.json` |
| Publish a signed manifest | `export KIT_ADMIN_TOKEN=...` then `python3 scripts/kit_ci.py publish --signed dist/ai-image.signed.json --api-url https://api.example` | `$env:KIT_ADMIN_TOKEN = '...'` then `.\scripts\windows\kit_ci.ps1 publish --signed dist\ai-image.signed.json --api-url https://api.example` |
| Lint a bring-your-own-container spec | `python3 scripts/check_byoc_image_spec.py spec.json --dockerfile Dockerfile` | `py -3 scripts\check_byoc_image_spec.py spec.json --dockerfile Dockerfile` |
| Smoke-test a Kit image (needs Docker and, for GPU Kits, an NVIDIA GPU) | `python3 kits/tools/smoke_runner.py --smoke kits/<kit>/smoke/smoke.json --image <image@digest> --report report.json` | Run it **inside the WSL2 runtime distro or on a Linux runner**: the script mounts POSIX paths and Kits are Linux images (X12) |

Key files on Windows: POSIX modes do not exist there. `sign_kit.py --gen-key` removes inherited permissions and grants only your
account with `icacls` (TODO(verify-docs): icacls switches; see the comment in `scripts/sign_kit.py`), and **deletes the new key again
if it cannot do that**. `sign_kit.ps1` refuses a key that Everyone, Users or Authenticated Users can read. The key stays on the signer
machine: never copy it to CI, a share or a chat.

## Vectors, smoke tests, load tests

| Task | bash | PowerShell |
|---|---|---|
| Generate the fan-out vectors and compare with the API | `python3 scripts/gen_fanout_vectors.py --write && python3 scripts/gen_fanout_vectors.py --check-app` | `.\scripts\windows\gen_fanout_vectors.ps1 -Write -CheckApp` (needs the API dependencies installed in the Python you run) |
| Check the vectors file is current | `python3 scripts/gen_fanout_vectors.py --check` | `.\scripts\windows\gen_fanout_vectors.ps1 -Check` |
| API smoke test (staging or production) | `./scripts/smoke-test.sh https://api-staging.computecoin.ai` | `.\scripts\windows\smoke-test.ps1 -ApiUrl https://api-staging.computecoin.ai` |
| API load test | `python3 scripts/staging-load-test.py http://localhost:8000 --concurrency 20 --requests 200` | `py -3 scripts\staging-load-test.py http://localhost:8000 --concurrency 20 --requests 200` |
| Seed the dev database (API dependencies, Linux or WSL2) | `python3 scripts/seed_dev.py` | run it in WSL2 (control plane, X3) |

## Host (provider) diagnostics

| Task | bash | PowerShell |
|---|---|---|
| Show detected hardware | `computecoin-agent detect` | `computecoin-agent detect` |
| **Support bundle**: a redacted diagnostics zip to attach to a ticket | `computecoin-agent support-bundle` | `computecoin-agent support-bundle` |
| Support bundle with chosen logs and output name | `computecoin-agent support-bundle --out ~/cc-support.zip --log-file /var/log/computecoin.log` | `computecoin-agent support-bundle --out "$env:USERPROFILE\cc-support.zip" --log-dir "C:\ProgramData\ComputeCoin\logs"` |

The bundle contains versions, the platform and capability report, hardware, the settings with every secret replaced, requirement and
runtime-health results (when this build has them) and the tail of the logs with secrets scrubbed. It contains no credentials, tokens,
keys, cookies, e-mail addresses, user names in paths or environment variables. **Before the bundle is written the scrubbed text is
checked again; if anything still looks like a secret nothing is written** and the command exits with code 2. Always open the zip and
read it before sending it. Code: `provider-agent/computecoin_agent/support_bundle.py`; tests: `provider-agent/tests/test_support_bundle.py`.

## Linux-only by design (not ported)

| Script | Why | On a Windows machine |
|---|---|---|
| `scripts/setup-multinode-net.sh` | Docker networks and host firewall rules for multi-node jobs (U13); multi-node is a no-go on Windows hosts for now (W8) | not applicable |
| `os/sealed/scripts/*.sh` (build the Sealed OS image, test keys, lock files) | builds a Linux disk image | run inside WSL2 or on a Linux build machine |
| `tests/qa/stack.sh` | local QA stack of Linux processes (Postgres, Redis, S3 mock) | run inside WSL2, or use `py -3 tasks.py up` |
| `apps/api/scripts/entrypoint.sh` | container entry point of the API image | not applicable (it runs inside the container) |

## Metrics and alerts (admin)

* Fleet metrics carry a `platform` label: `computecoin_ops_machines_by_platform{availability,platform}` (`linux`, `windows`, `unknown`:
  `unknown` means the host reported nothing). The older `computecoin_ops_machines{availability}` is unchanged.
* Windows gauges: `computecoin_ops_platform_distro_unhealthy`, `..._gpu_smoke_failing`, `..._update_rollbacks`, `..._hosts_yielding`
  (label `platform`). They count hosts that **reported** the condition; a host that reports nothing counts as zero, never as a failure.
* Rules: group `computecoin-windows` in `infrastructure/observability/alerts.yml`. Dashboard panels: `infrastructure/observability/grafana/computecoin-platform.json`.
* The heartbeat keys these read (`health.runtime.distro_ok`, `health.runtime.gpu_smoke_ok`, `health.update.rolled_back`,
  `health.availability_detail.state`) are a contract with the supervisor and updater and are **unconfirmed until those report them**.
