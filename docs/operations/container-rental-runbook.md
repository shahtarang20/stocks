# Container rental runbook: turning it on and proving it on a real GPU

Written 2026-11-15. Design and contract: [../architecture/RENTAL-container-rental-design.md](../architecture/RENTAL-container-rental-design.md).
**Status: built and tested with fakes and test clients; never run on a real GPU.** Everything below that says "check" is yours to do.

## 1. What this is, in one paragraph

A customer picks a GPU on the marketplace, picks a ready template (PyTorch or vision, with JupyterLab) or brings their own public image, and
gets Jupyter or a terminal on that GPU. They have a workspace while the rental runs, can pull their stored files in, and save results out. Billing
is the existing per-GPU-second session billing. It is a second path beside Kits, off by default on both sides: the **platform** switch and the
**host** opt-in must both be on.

## 2. What the owner turns on

| Switch | Where | Value |
|---|---|---|
| `CONTAINER_RENTAL_ENABLED` | Railway, API service, Variables | `true` (default `false`: rentals are refused and templates hidden) |
| `RENTAL_ALLOWED_REGISTRIES` | same | optional; default `docker.io,ghcr.io,nvcr.io,quay.io` |
| `RENTAL_EGRESS_ALLOWLIST` | same | optional; hosts a rental may reach (default: Python packages, PyTorch, Hugging Face, GitHub, Conda) |
| `SESSIONS_RELAY_BACKEND` | same | **`redis`** (the API runs more than one worker; the in-memory relay breaks with two). If rental is on, the API has more than one worker and this is not `redis`, it logs an error at start and **refuses new rentals** (`session_relay_not_ready`) instead of starting ones nobody can connect to |
| `STREAM_RELAY_BACKEND` | same | `redis` (same reason, for streamed model output) |
| `WINDOWS_RENTAL_ENABLED` | same | **EXPERIMENTAL**, default `false`. `true` lets a Windows host keep the rental flag and be handed rentals (never Kit sessions). Needs `CONTAINER_RENTAL_ENABLED` too. Leave off until a Windows PC has passed [the first real test](windows-rental-test-checklist.md) |
| `WINDOWS_RENTAL_MACHINE_IDS` | same | **EXPERIMENTAL PILOT**, default empty. Comma-separated machine ids (the id in the My computers page link or the marketplace) that may offer Windows rentals while `WINDOWS_RENTAL_ENABLED` is `false`: use it to try ONE real Windows PC first. Junk entries are ignored. Needs `CONTAINER_RENTAL_ENABLED` and the host's own opt-in too |
| `NEXT_PUBLIC_SESSION_PROXY_ORIGIN` | Vercel, web project | the address of a **second** deployment of the web app on another domain (needed to open Jupyter in the browser; see the U10 design, section 9). While it is unset the Rent page hides the templates and the web-page option and offers only a terminal on the customer's own image |

`REDIS_URL` must already be set (it is). The Redis relay code was written from documentation and checked only against a fake Redis; watch the first
real session.

## 3. Build the templates (once)

1. GitHub, **Actions**, **Rental templates**, **Run workflow**, tick **Build AND push**. It builds `pytorch-jupyter` and `vision-jupyter` for
   `linux/amd64` and prints each image's pinned reference in the run summary.
2. On your computer: `python scripts/rental_fill_digests.py --slug pytorch-jupyter --image ghcr.io/<owner>/rental-pytorch-jupyter@sha256:<digest>`
   (and the same for `vision-jupyter`). It refuses anything unpinned or from a registry that is not allowed.
3. Commit `apps/api/app/data/rental_templates.json` and push. A template with no digest stays hidden and refused.
4. On GitHub, open each `rental-<name>` package, **Package settings**, **Change visibility**, **Public**, so a host's Docker can pull it without
   logging in.

## 4. A host opts in

**First cut a new agent release.** The `rental` command and the rental code exist only in this repository so far. The version is already 0.2.0
(`provider-agent/pyproject.toml` and `computecoin_agent/__init__.py`); run the agent release job and have hosts reinstall or upgrade. An agent
installed from 0.1.0 has no `rental on`, never reports the flag, and is never handed a rental; the host page shows it as "Update agent for container
rental". The installer's `--allow-rental` tolerates an old agent (it warns and goes on).

On the host computer, first prove it can do the job: **`computecoin-agent rental check`**. It tests Docker, the graphics card, free disk, the network
filter, and runs a locked-down test container (read-only, no extra rights, no network, the GPU attached) that writes to a workspace folder like a
rental's, then deletes it. It says in plain words what to fix and exits non-zero on a failure. The first run pulls a small public CUDA image (or use
`--image <public image>` to test the one you will serve). Then: `computecoin-agent rental on` (or install with `--allow-rental`). `computecoin-agent rental status` shows the setting and
`computecoin-agent rental off` turns it off. After the next heartbeat the machine reports the `container_rental` flag; the host console shows
"Container rental: on" and the marketplace shows the badge.

The host must understand: a stranger's code runs on their computer in a locked container (no extra rights, no network unless the customer asked
for allowed software sites, a private work folder that is deleted at the end). It is not a private environment, so it is for non-sensitive work.
Disk: each rental gets at most `rental_max_workspace_gb` (default 50) of the host's disk while it runs.

**Network filter.** A rental that asks for package sites (both templates do) needs a filtering proxy image on the host. Agent 0.2.0 has the
project's own image built in (`DEFAULT_EGRESS_PROXY_IMAGE` in `provider-agent/computecoin_agent/config.py`, built by the **Workload images**
workflow, job "Build egress proxy", pinned by digest), so a host needs **no setting**. A host may replace it (`egress_proxy_image` in `agent.toml`,
or `COMPUTECOIN_EGRESS_PROXY_IMAGE`) or turn it off with an empty value; a host that turned it off reports no `egress_proxy` flag, the API never
hands it such a rental, and refuses one aimed at that machine (`machine_cannot_filter_network`). `rental check` reports it as a warning.
To change the image: rebuild it (needs the repository variable `PROXY_BASE_IMAGE`), put the new digest in `DEFAULT_EGRESS_PROXY_IMAGE`, and release a new agent.

## 5. First real test (you, on your GPU computer, with a test account)

Do it in this order and stop at the first failure:

0. On the host: `computecoin-agent rental check` passes (warnings are allowed, FAIL is not). It prints an **NVIDIA driver** line (535.86 or newer
   is needed) and picks its test image from the driver (CUDA 12.4 for 550 and up, CUDA 12.2 for 535 to 549): write both down.
   A laptop with an AMD processor and an NVIDIA card shows two graphics brands: the installer now uses the NVIDIA card and says so.
   For the first GPU rental, also run `nvidia-smi` and `python3 -c "import torch;print(torch.cuda.is_available())"` in the PyTorch template
   (it must print `True`), and pull the plug on nothing: a "driver version is insufficient" or "Failed to initialize NVML" message is a known
   cause, with the fix in the host guide.
1. Opt the host in, wait a minute, and check the marketplace shows **Container rental** on that machine.
2. As a verified test customer with test credits, open **Rent a GPU**, pick that machine and `pytorch-jupyter`, accept the warning, start it.
3. Open Jupyter from the session page. In a notebook run `import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))`. Expect `True` and your card.
4. Train something small. Save a file to `/workspace/results`, press **Save results now**, and check it appears under the session's saved files and downloads.
5. Rent again with an input file chosen from your storage; check it is at `/workspace/<path>` before Jupyter opens.
6. Check the limits: fill the workspace past its size (the rental should stop with `workspace_full` and still save `results`), then stop the rental and
   confirm the container and the workspace folder are gone from the host (`docker ps -a`, and the `rental` folder under the agent's data folder).
7. While a rental runs, check the host cannot start a normal job on the same GPU (the job claim should refuse), and that the heat watchdog still works.
8. Check billing: the customer is charged for the active seconds only; the host earns; an unclaimed or failed-to-start rental charges nothing.

## 6. Limits you should know

* **Not private.** The host can see the customer's files and processes. Customers must accept a warning at rent time; it is stored.
* **Not durable, but results are saved as you go.** If the host dies, the workspace is lost. Every 5 minutes the agent saves changed files from
  `/workspace/results` to the customer's storage (and again when the rental ends), so at most about 5 minutes of results are at risk. Anything
  outside `results` is not saved. Unchanged files are not sent again (judged by size and modified time).
* **No SSH, no inbound ports, no private-registry images.** Jupyter and a terminal through the encrypted relay only.
* **Abuse.** The agent stops a rental whose processes look like a cryptocurrency miner (a name list; it is a deterrent, not a guarantee). Network is
  off unless the customer asks for hosts from your allow-list. Keep `CONTAINER_RENTAL_ENABLED` off if you do not want to carry this risk.
* **One GPU user at a time.** A machine with a running job does not take a rental, and a machine with a live rental takes no job.
* **Disk quota is checked every 30 seconds**, not enforced by the file system, so a fast writer can overshoot for a moment.
* **Face images and other personal data** are biometric or personal data. The host can read them. Do not offer this for such data without legal advice.
* **Downloads resume.** A dropped input download continues where it stopped (in 1 MiB steps) and is still checked against the stored checksum.
* **The templates are untested images.** Package versions were checked to exist and to resolve together for Python 3.11; the images themselves have
  never been built or run.

## 7. If something goes wrong

* A rental stuck in `pending`: no opted-in machine matched (price too low, machine busy with a job, wrong machine picked). It is released
  automatically after 10 minutes with nothing charged.
* A rental that never opens Jupyter: look at the host's agent log (`journalctl -u computecoin-agent`) for the container start error; check the image
  is public and the digest is right.
* Turn everything off at once: set `CONTAINER_RENTAL_ENABLED=false` in Railway. Live rentals finish; no new ones start.
