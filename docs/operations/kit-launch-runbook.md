# Kit launch runbook: getting real AI and GPU work onto the platform

Written 2026-11-14. **Status: parked.** Nothing in this file has been done yet. It records what was checked, what is missing, and the exact steps,
so the work can be picked up later without re-investigating.

## 1. Where things stand (checked on 2026-11-14)

| Fact | Evidence |
|---|---|
| The live API has **no Kits published** | `GET https://stocks-production-76fe.up.railway.app/v1/kits` returned `[]` |
| The live marketplace has no hosts yet | `GET /v1/marketplace/machines` returned 0 items |
| The only model a customer can run today is the **practice model `echo-sim-v1`**, which does not run real AI | It is created by migration `0004_jobs_phase4.py`; the job form defaults to it |
| 15 Kits are written in `kits/` but **never built or run** | Every `kits/<slug>/kit.json` still has an image digest of `PENDING`; `kits/README.md` says "code written, never built or run" |
| The Kit unit tests pass | `pytest kits/tests`: 78 passed, 13 skipped (local, Linux) |
| TinyLlama is registered as an approved model but its image cannot be relied on | Migration `0009_hardware_model_phase4.py` points at `ghcr.io/computecoin/model-runners/tinyllama-v1@sha256:4a38...`; `workloads/digests.json` still has `null` digests; the image build job (`workload-images.yml`) has never run |
| The agent has **never run a job on a real graphics card** | `RUN_GPU_TESTS` tests are skipped everywhere; the real-GPU path is untested |
| The main CI workflow is red | Has failed on every run since at least 2026-11-05; the Windows `kits` job fails at "Kit unit tests" (Windows environment); not caused by Kit work |

## 2. The ways a customer can run something real

| Route | How it works | Admin work | State |
|---|---|---|---|
| **Kits** (the product's self-service route) | You publish signed Kits once. Customers pick a Kit and fill in its parameters. No admin step per job. Hosts re-run a sample on a second host to check results | One-time per Kit: choose model and licence, build the image, pin hashes, sign, publish | Code written, never built |
| **Approved model** | A model record plus a pinned image digest. Customers type the model name | Create the model record (**no admin endpoint exists**: it needs a database change like migration 0009) and register the digest with `POST /v1/admin/model-versions/<tag>/images` | Works in code; TinyLlama image missing. A public image means public weights, and an approved model is open to every customer, so it is wrong for a proprietary model |
| **Bring your own container (BYOC)** | The customer supplies `image@sha256:...`; the platform scans it and runs it in a sandbox | Configure an image scanner (Trivy or ClamAV) and an allowed-registries list on the API; hosts need gVisor or Kata (graphics cards inside gVisor are still limited) | Blocked on the live system: fails closed with no scanner |
| Self-service model submission with automatic checks and a test run | Not in the code | Needs a design note and your approval first (plan decision L8) | Not built |

**Recommended order:** publish one or two Kits first (suggested: batch embeddings, plus either image generation or a small LLM), then add more.
All 15 at once is not realistic: each needs a model and licence choice, a build, and a real-GPU test; notebooks need interactive sessions, LLM serving
needs the services feature, and fine-tuning wants several GPUs.

## 3. What "done" means for one Kit

1. Code audited, unit tests pass.
2. Base image pinned by digest; weights pinned by hash (`ASSET_URL`, `ASSET_SHA256`); licence recorded in the Kit's `README.md`.
3. The image builds on GitHub (Actions workflow `Kits`) and is pushed to `ghcr.io`, then set **public**.
4. A smoke test passes on a real graphics card (your GPU computer).
5. The manifest is signed with your private key and published to the API.
6. A test customer runs it from the website and gets a correct result.
7. The Kit appears in `GET /v1/kits`.

## 4. Where each piece lives

| Piece | Where |
|---|---|
| Kit code (`kit.json`, `Dockerfile`, `entrypoint.py`, schema, smoke spec) | This repo, `kits/<slug>/` |
| Image build | GitHub Actions, workflow `.github/workflows/kits.yml` (manual run with `push_images`), pushed to `ghcr.io`. **Not on a laptop** (CUDA images are many gigabytes and a local build writes to Docker's storage outside the project) |
| Model weights | Downloaded during the GitHub build from an address you choose, then baked into the image or a hash-pinned archive. **The licence is your decision** |
| Real-GPU smoke test | A machine with a graphics card, registered as a **self-hosted GitHub runner** with a label for the card. The workflow currently asks for the labels `gpu-3090`, `gpu-4090`, `gpu-a100` (three card types) and `cpu-a`, `cpu-b`, `cpu-c` for the CPU Kit; it must be adapted to the labels you actually have |
| Signing | A computer you control, with `scripts/sign_kit.py` and your **private key**. Never in CI and never on a server |
| Public key | Railway, API service, variable `KIT_SIGNING_PUBLIC_KEY` (64 hex characters). Unset means no Kit can be published or used |
| Published Kits | The production database, through `POST /v1/admin/kits` |

## 5. Steps, in order

Replace `<api>` with `https://stocks-production-76fe.up.railway.app`.

### 5.1 Choose the first Kits (a decision, not a command)
The GitHub variables below depend on the choice. For each Kit you need: a base-image digest, the weights download address and its SHA-256, and the
confirmed licence. The full list in the workflow header is for all Kits and is far longer than a pilot needs.

### 5.2 Generate the signing key (on your own computer)
```
cd /home/root509/stocks
provider-agent/.venv/bin/python scripts/sign_kit.py --gen-key ~/kit-signing.pem
```
It writes the private key (readable only by you; it refuses to overwrite an existing file) and prints `KIT_SIGNING_PUBLIC_KEY=<64 hex characters>`.
Copy the public key. Keep the `.pem` **outside the repo**, make **two backups** (a password manager and an offline encrypted drive), and never commit,
email or paste it.

### 5.3 Tell the API the public key
Railway, the API service, **Variables**, **New Variable**: name `KIT_SIGNING_PUBLIC_KEY`, value the public key. After the redeploy, check
`<api>/health` says `ok`.

### 5.4 GitHub settings
- Repo, **Settings**, **Secrets and variables**, **Actions**, **Variables**: add `KIT_API_URL` = `<api>` and the Kit variables from 5.1.
- After the first build, set each `kit-<slug>` package's visibility to **Public** (package settings, **Change visibility**) so hosts can pull it
  without logging in. New packages are usually private; check.
- Your GPU computer as a runner: repo, **Settings**, **Actions**, **Runners**, **New self-hosted runner**, Linux, run the commands shown on that
  computer, and give it a label for the card (for example `gpu-4090`).
- The repo is public, so under **Settings**, **Actions**, **General** require approval for workflows from outside contributors. The smoke jobs only run
  on a manual start, but a self-hosted runner on a public repo should never run code from strangers.

### 5.5 Build and test
**Actions**, then **Kits**, then **Run workflow**: tick **Build AND push images**, leave **publish** unticked. It builds on GitHub, then smoke-tests on
your GPU computer. When it is green, download the `manifest-<slug>` artifacts into a `dist` folder.

### 5.6 Sign and publish (by hand, from your computer)
```
python scripts/sign_kit.py --manifest dist/<slug>.json --key ~/kit-signing.pem --out dist/<slug>.signed.json
read -s KIT_ADMIN_TOKEN; export KIT_ADMIN_TOKEN        # paste an administrator access token (typing is hidden)
python scripts/kit_ci.py publish --signed dist/<slug>.signed.json --api-url <api>
```
- Get the token by logging in as an administrator (`POST <api>/auth/login`). **It expires after 15 minutes** (`ACCESS_TOKEN_TTL_SECONDS` is 900), so
  fetch it right before publishing.
- That is why the workflow's automatic `sign-publish` job (which needs a stored `KIT_ADMIN_TOKEN` secret, a self-hosted `kit-signer` runner and a
  protected `kit-signing` environment) is not used for a pilot: a stored token would already be stale.
- `kit_ci.py publish` treats HTTP 409 as "this version is already published" and leaves it alone: versions are immutable.

### 5.7 Check
`<api>/v1/kits` lists the Kit. Then run one job as a test customer from the website and confirm the result.

## 6. Changing laptop, or losing the key

- Only the signing key lives on the laptop. The code is on GitHub, the live settings are in Railway, the website is on Vercel, email is Resend, the
  runner is on the GPU computer.
- **New laptop:** install git and Python 3.12, `git clone https://github.com/shahtarang20/stocks.git`, log in to your accounts, copy the key from a
  backup (never by email or chat), `chmod 600 ~/kit-signing.pem`, and check it is the right key by signing a throwaway file; the script prints the
  public key on stderr, which must equal `KIT_SIGNING_PUBLIC_KEY` in Railway:
  ```
  echo '{}' > /tmp/t.json
  python scripts/sign_kit.py --manifest /tmp/t.json --key ~/kit-signing.pem > /dev/null
  ```
  Then delete the key from the old laptop and revoke its tokens.
- **Key lost with no backup:** the API trusts **one** public key (`KIT_SIGNING_PUBLIC_KEY`) and re-verifies a stored Kit against it every time it is used
  (`assert_kit_intact`). After changing the key, every Kit signed with the old one fails until re-signed, and a published version cannot be replaced, so
  each Kit must be republished under a **new version number**. Customers cannot run Kits in the meantime.
- **Option to build later:** let the API trust several public keys at once, so a new key can be introduced while old Kits stay valid. It touches how
  Kits are verified, so it needs a short design note and your approval first.

## 7. Decisions still open (yours)

- Which Kits first, which models, and each model's licence (record it in the Kit's `README.md`).
- Signing key custody (laptop, hardware token, password manager) and who may use it.
- Whether to build the multi-key support in section 6.
- Whether verification sampling stays on (`KIT_VERIFICATION_SAMPLE_RATE` defaults to 0.02): the Kit READMEs mark thresholds as owner decisions and say not to
  publish with redundancy sampling on until you decide.
- Whether the first release uses Kits only, or also the approved-model route for something like TinyLlama.

## 8. Other things to settle before real hosts and users (not Kit work)

- **Email:** `EMAIL_FROM` is `onboarding@resend.dev`, Resend's test sender, which only delivers to the address you signed up to Resend with. Verify your own
  domain in Resend (or use Gmail SMTP as a stopgap). Until then use the admin Users page, **Confirm email**, for people you know.
- **Payments:** `PAYMENT_PROVIDER` defaults to `simulated` (test credits). Customers add test credits only while it is not `stripe`.
- **Installer:** never run as root on a real Linux machine; try it once on a spare computer. A host needs Linux and Docker (and the NVIDIA driver and
  container toolkit for a graphics card).
- **Monitoring:** Sentry is disabled; errors are only in Railway's logs.
- Pilot of 3 hosts and 3 customers is possible today **with the practice model only**; see `docs/operations/agent-release-and-install.md` for the host
  install and `docs/PLATFORM_GUIDE.md` for the host and admin guides.

## 9. Related files

- `kits/README.md`: how a Kit is authored, the container contract, the placeholder digests.
- `.github/workflows/kits.yml`: the build, smoke test and sign-publish pipeline (never run).
- `scripts/sign_kit.py`, `scripts/kit_ci.py`: signing and publishing.
- `apps/api/app/services/kits.py`: signature verification and the stored-Kit integrity check.
- `docs/architecture/U1-job-spec-v2-design.md`, `docs/architecture/U8-byoc-design.md`: Kit and BYOC design.
- `docs/operations/remaining-code-work-master-plan.md`, section 8: the owner decisions the plan waits on.
