# ComputeCoin — Complete Platform Guide

**Every page, every feature, for all three roles.**

This guide covers the ComputeCoin web console exactly as it is built today. Each
section explains what a page is for, what you can do on it, how to do it, and
what to watch out for.

| Role | Who it is | Where they work |
|---|---|---|
| **Customer** | A developer who needs GPU compute | `/dashboard/*` |
| **Host (Provider)** | Someone who owns a GPU and rents it out | `/provider/*` |
| **Admin** | The platform operator | `/admin/*` |

One account can hold more than one role. If you hold several, links to the other
consoles appear at the bottom of the sidebar.

---

## Read this first

Four facts that change how you should interpret what you see. They are stated
here rather than buried, because the console deliberately labels them too.

**1. Jobs currently execute in simulated mode.**
Every job carries an `execution_mode` field, and today it reads `simulated`.
The real hardware path is built — the agent passes `--gpus`, detects the NVIDIA
toolkit, and a `tinyllama-1.1b-chat` model is registered with
`runtime: hardware` — but no job has yet run on a physical GPU. Treat any output
as a simulation until you see `execution_mode: hardware` on the job detail page.

**2. Balances are simulated credits.**
Amounts are shown verbatim in `USDC_CREDITS` and are never converted to fiat.
The ledger arithmetic behind them is real double-entry accounting; the currency
is not yet real money.

**3. Machine capability is self-reported.**
When a host enrols a machine, the agent reports its own hardware. The console
labels this **Detected**, never "Verified". A machine claiming eight H100s has
claimed it, not proven it.

**4. Failed work is not charged.**
A job that fails, is cancelled or times out releases its hold in full. Only a
job that reaches `completed` is captured. You can verify this yourself on the
Billing page — every movement appears in the ledger.

---

## Getting started (all roles)

### Create an account — `/register`

Enter an email and a password. Passwords are hashed with Argon2id; the plaintext
is never stored.

### Verify your email

Registration sends a verification link. **You must verify before you can spend
credits or submit a job** — an unverified account is blocked from both at the
API level, not just hidden in the UI.

If the email does not arrive, check the Resend quota (Admin → Third-party
services). A exhausted email quota breaks signup silently.

### Sign in — `/login`

Sessions use httpOnly cookies plus a CSRF double-submit token. Access tokens are
short-lived and refresh automatically in the background. Nothing sensitive is
kept in browser storage.

### Settings — `/dashboard/settings`

Your profile and current session. Sign out here to clear all auth cookies.

---

# PART 1 — Customer guide

Everything a developer does, in the order you will actually do it.

---

## 1.1 Dashboard — `/dashboard`

Your landing page: current credit balance, recent jobs, and quick links.

**Use it to** check at a glance whether you have budget and whether anything
recent failed. Start here each session.

---

## 1.2 Projects — `/dashboard/projects`

> *Organize API keys, budgets, and workloads into isolated projects.*

A project is a container. Jobs and API keys belong to one, which keeps work
separated — for example `staging` and `production`, or one project per client.

**What you can do**

| Action | How |
|---|---|
| Create a project | **New project**, give it a name |
| Delete a project | Delete on the project row |

**How to use it well.** Create at least one project before generating API keys.
Jobs submitted to a project are attributed to it, so per-project usage reporting
only works if you actually separate them. Deleting a project is not a way to
delete its history — ledger entries and invoices persist, as they must.

---

## 1.3 API keys — `/dashboard/api-keys`

> *Manage project-scoped API keys for programmatic access to the ComputeCoin API
> and OpenAI-compatible inference endpoints.*

**What you can do**

| Action | How |
|---|---|
| Create a key | **Create key**, choose the project it belongs to |
| Revoke a key | Revoke on the key row — takes effect immediately |

**Critical:** the key is shown **exactly once**, at creation. Only a hash is
stored, so nobody — including the platform — can recover it afterwards. Copy it
immediately into your secret manager. If you lose it, revoke it and create
another.

**How to use it.** Send the key as a bearer token:

```bash
curl https://<your-api>/v1/jobs \
  -H "Authorization: Bearer cc_live_..."
```

Two endpoints are OpenAI-compatible, so an existing OpenAI client works by
changing the base URL and key:

| Endpoint | Equivalent to |
|---|---|
| `POST /v1/chat/completions` | OpenAI chat completions |
| `POST /v1/embeddings` | OpenAI embeddings |

Responses from these are currently stamped `[ComputeCoin Simulated]` in the
output text itself — they are deterministic simulations, not real inference, and
say so rather than pretending otherwise.

**What a key can and cannot do.** A key acts as its project's **owner** and
nothing more. It is not an elevated credential: an admin-only endpoint still
returns 403, and the email-verification gate still applies, so a key cannot be
used to sidestep it. Revoking a key stops it immediately, and **suspending the
owning account kills every key it issued** — a banned account is left with no
live credential.

Scope keys per project so revoking one does not break everything. The key list
shows **last used**, which is how you spot a key that is unused (revoke it) or
being used when it shouldn't be (rotate it).

---

## 1.4 Browse GPUs — `/marketplace/gpus` *(public)*

The marketplace is public — you can browse before signing in.

**What you see** per machine: GPU model, VRAM, CPU and RAM, region, price per
hour, supported workloads, and verification status.

**What to watch.** A machine only appears here when it is online, available,
priced, and has sent a recent heartbeat. A host that disconnects drops off the
list within the heartbeat window. Capability reads **Detected** — self-reported.

**How to use it.** Note the machine ID if you want to target a specific host.
Otherwise skip this entirely and let the scheduler choose.

---

## 1.5 Submit a job — `/dashboard/jobs/new`

The core action. Every field, in form order:

| Field | What it does | Guidance |
|---|---|---|
| **Workload type** | `text-generation` or `batch-inference` | Must match what the model supports |
| **Isolation tier** | Container hardening level | Leave as `container` unless told otherwise |
| **Model** | e.g. `echo-sim-v1` | This is the **version tag**, not the model name. An unregistered tag is rejected with `model_not_supported` |
| **Prompt** | Your input text | Required for `text-generation` |
| **Max tokens** | Output length cap | Optional |
| **Items (JSON)** | A JSON list | Required for `batch-inference` instead of a prompt |
| **Target machine** | Pin to one machine | *Leave blank to let the scheduler pick* — usually better |
| **GPU count** | GPUs required | Must fit the machine, or you get `gpu_count_exceeds_machine` |
| **Est. minutes** | Expected runtime | Drives the quote and the size of the hold |
| **Max runtime (s)** | Hard timeout | The job is killed past this |
| **Max retries** | Automatic retry attempts | 0 means fail fast |
| **Max cost (budget cap)** | e.g. `5.00` | **Set this.** It is your hard ceiling |

**What happens when you submit**

1. **Validation** — model, workload and input shape are checked.
2. **Matching** — the scheduler scores machines on price, availability and real
   reputation built from completion history.
3. **Reservation** — a GPU slot is locked. Under concurrency exactly one
   customer wins a contested slot; this is proven by test, not assumed.
4. **Hold** — credits are *held*, not charged.
5. **Execution** — the host's agent claims the job and runs it in a hardened
   container: no host filesystem, no Docker socket, all capabilities dropped,
   network disabled, with memory, CPU and process limits.
6. **Result** — output and artifacts are uploaded.
7. **Capture** — the hold converts to a real charge. Unused budget is released.

**Common rejections**

| Error | Meaning |
|---|---|
| `model_not_supported` | Wrong model tag, or the model doesn't support that workload |
| `invalid_input` | `text-generation` needs `prompt`; `batch-inference` needs `items` |
| `gpu_count_exceeds_machine` | Asked for more GPUs than the target has |
| `insufficient_credits` | Not enough balance for the hold |
| `email_not_verified` | Verify your email first |
| `spend_limit_exceeded` | A configured spend cap would be breached |

---

## 1.6 Jobs list — `/dashboard/jobs`

> *Completed jobs show their real settled cost; failed or cancelled jobs are
> never charged.*

Every job with its status and cost. Use it to find a job, then open it.

### Job states

| State | Meaning |
|---|---|
| `created` | Accepted, not yet validated |
| `validating` | Checking model, input and limits |
| `queued` | Waiting for a machine |
| `matched` | A machine has been chosen |
| `reserved` | Slot locked, credits held |
| `running` | Executing on the host |
| `verifying` | Result being checked |
| `completed` | Done and charged |
| `failed` | Did not finish — **not charged** |
| `cancelled` | You stopped it — **not charged** |
| `timed_out` | Exceeded max runtime — **not charged** |
| `retry_pending` | Will be retried |
| `disputed` | Under dispute |

---

## 1.7 Job detail — `/dashboard/jobs/[jobId]`

The richest page in the console.

**What you see**

- Full status and state history (every transition, who caused it, when)
- `execution_mode` — **check this**; `simulated` means no real GPU ran it
- Live logs
- Output artifacts, each downloadable via a short-lived signed link
- Final cost, and the match explanation for why this machine was chosen

**What you can do**

| Action | When | Effect |
|---|---|---|
| **Cancel** | While running | Stops the job; the hold is released |
| **Download artifact** | After completion | Signed, expiring, single-scope URL |
| **Open dispute** | After completion | Flags the result for admin review |

**Opening a dispute.** If a completed job produced wrong or empty output, open a
dispute here. The system automatically attaches the evidence — inputs, outputs,
hashes, verification and redundancy-check results — so an admin can judge
without you having to assemble a case. You do not need to argue; you need to
flag it.

---

## 1.8 Reservations — `/dashboard/reservations`

> *Reservations hold a slot before job execution; no credits are charged here.*

Capacity slots you hold.

| Action | Effect |
|---|---|
| **Release** | Frees the slot back to the marketplace |

**Watch for** reservations you no longer need. They occupy capacity. The
platform sweeps genuinely dead reservations automatically, but releasing yours
promptly is good practice.

---

## 1.9 Usage — `/dashboard/usage`

> *Estimated vs actual cost, tracked separately.*

Per-job metered usage. The two columns matter: **estimated** is what the quote
predicted, **actual** is what metering measured. Persistent large gaps mean your
`est. minutes` values are wrong — fix them, because estimates size your holds
and an oversized hold blocks budget you could be spending.

---

## 1.10 Billing — `/dashboard/billing`

> *Live balance and double-entry ledger.*

**What you can do**

| Action | How |
|---|---|
| **Buy credits** | Enter an amount and purchase |
| **Read the ledger** | Every transaction, in full |

**Reading the ledger.** Three figures: **available**, **reserved** (held against
running jobs) and **balance**. Reserved money is not spent — it is ring-fenced
and returns if the job fails.

Entry kinds you will see: `credit_purchase`, `hold`, `capture`, `release`,
`refund`.

This is a real double-entry ledger: debits always equal credits. Nothing moves
except through a balanced, logged transaction, and no account that should stay
positive can go negative — including under concurrent submissions.

---

## 1.11 Invoices — `/dashboard/invoices` and `/[invoiceId]`

Itemized invoices per settled job. Open one for the breakdown; download it for
your records. Estimated and actual are itemized separately, so you can see
exactly what you were charged for and why.

---

## 1.12 Disputes — `/dashboard/disputes`

> *Disputes you have opened on settled jobs.*

Track your disputes and their outcomes. You open them from a job's detail page,
not here.

**Outcomes:** upheld (refunded), rejected (charge stands), or partially
refunded. Resolution is an admin action and every step is audit-logged.

---

## 1.13 Rent a GPU (container rental) — `/dashboard/rent`

> *Run your own code, such as training or fine-tuning a model, on a host's graphics card, with JupyterLab or a terminal.*

This is a second way to use a GPU, beside Kits. You pick a computer from the list (only computers whose host chose to allow
container rental appear), then either a **ready-made template** (PyTorch, or a vision stack with OpenCV and face-recognition
libraries) or **your own public image**, accept the warning, and start. The session page opens JupyterLab or a terminal through the
encrypted relay. You pay per second while it is active, the same way as any session; a rental that never starts costs nothing.

* **No graphics card (CPU only).** Choose **0** under Graphics cards to use only the computer's processor and memory, for a small test or work that needs no card. It is charged at the computer's hourly price, as for one card, and graphics-card code will not use a GPU.
* **Workspace.** A private folder (`/workspace`) on the host's disk, sized by you (up to the platform limit). It lasts only while the
  rental runs and is deleted when it ends.
* **Files in.** Choose files from your stored files; they are copied into the workspace before the rental starts, and that time is not charged.
* **Results out.** Put finished files in `/workspace/results`. They are saved to your storage automatically every few minutes, when you press **Save results now**,
  and when the rental ends, so if the host's computer dies you lose at most the last few minutes. Saved files appear on the session page and in your stored files.
* **Internet.** Off by default. Tick only the package sites you need (for example pip).
* **Custom images.** Public images only, from Docker Hub, ghcr.io, nvcr.io or quay.io, pinned by `@sha256:` digest.
* **The warning.** The host can see your files and processes. This is for non-sensitive work: do not use it for secrets, private models you
  cannot afford to expose, or personal data such as face images of real people without a legal basis. For that, use Kits, sandboxed hosts or the private tiers.

Container rental is **off by default** on the platform and must also be on for the host. Nothing here has been run on a real GPU yet; see
[container-rental-runbook.md](operations/container-rental-runbook.md).

---

# PART 2 — Host (Provider) guide

How to rent out your GPU and get paid.

---

## 2.1 Onboarding — `/provider/onboarding`

> *These steps run live against the ComputeCoin API.*

Three steps, in order.

### Step 1 — Register as a provider

Give a display name and contact email. This creates your provider profile.

### Step 2 — Mint an enrollment token

Click to create a **one-time** token. It is shown once and expires.

**Treat it like a password.** Anyone holding it can enrol a machine under your
account. Revoke it immediately if it leaks — there is a revoke action on the
token list.

### Step 3 — Connect your machine

**The easy way (Linux): one line.** The setup page shows a single line to paste into a terminal on the computer (it saves the installer to a temporary file and runs it with `sudo`, so it keeps your
terminal for one question). It asks for the one-time token (typing hidden; the setup page shows the token to paste),
checks the computer (Docker, graphics card), installs the agent in its own folder with every dependency hash-checked, connects the computer, and installs and starts
services that restart after a crash or reboot: the agent, its heat watchdog and, on NVIDIA, a tiny root-only power cap service. For scripts that cannot answer a prompt, add `--token enr_...` (visible to other users while it runs). It is safe to run again (it upgrades), `--dry-run` shows what it would do, `--uninstall` removes it. It does not
install Docker or graphics drivers. Options: `--reenroll` (connect again after removing the computer on the website), `--gpu-vendor nvidia|amd`, `--force` (upgrade while a customer job is running), `--run-as-root`. How it works and how a new agent version is released: [operations/agent-release-and-install.md](operations/agent-release-and-install.md).

**By hand (advanced):** install the agent on the GPU host and give it the enrollment token. The agent:

1. Detects your real hardware (GPU model, VRAM, CPU, RAM, disk)
2. Enrols with the token
3. Receives a **machine secret** — shown once, used to sign every request
4. Begins sending signed heartbeats

Every agent request is HMAC-signed. A forged signature or a tampered body is
rejected with 401 — verified by test.

### Hardware & Software Requirements for Providers

#### NVIDIA Hosts
- **Docker Engine**: Docker CE >= 24.0.
- **NVIDIA Driver**: Version >= 535.86.
- **NVIDIA Container Toolkit**: Installed and configured as Docker default runtime (`nvidia-ctk runtime configure`).

#### AMD Hosts (ROCm)
- **ROCm Version**: ROCm 6.1.x or 6.2.x. Intended for RDNA3 (Radeon RX 7900 series) and CDNA (Instinct MI300X/MI250). AMD support has not yet been tried on a real AMD card (see the real-hardware checklist in `operations/amd-gpu-support-plan.md`).
- **Driver & Groups**: Kernel driver with `/dev/kfd` and render nodes (`/dev/dri/renderD*`). The agent user must belong to both `render` and `video` groups (`sudo usermod -aG render,video $USER`).
- **Agent AMD Extra**: The agent must be installed with the optional `amd` extra (`pip install -e ".[amd]"`), which pulls in `amdsmi` matching the host's ROCm version.
- **Mixed-Vendor Hosts (D2 Policy)**: ComputeCoin requires exactly one GPU vendor per machine. If a host contains both NVIDIA and AMD GPUs, enrollment is refused unless `gpu_vendor = "nvidia"` or `gpu_vendor = "amd"` is pinned in `computecoin.toml` (or via `--gpu-vendor`).
- **Isolation Tier (D5)**: AMD GPU jobs run under the `container` tier by default. gVisor, microVM and Confidential are always refused on AMD (Confidential needs a GPU confidential mode that current AMD GPUs do not have). Sealed and Sealed Pro on AMD are coded but OFF by default, behind three locks that must all be open: the server setting `AMD_SEALED_ENABLED`, a current attestation verdict that names a Sealed image for the machine, and the host's own `amd_sealed_enabled` agent setting. This path has not been proven on real hardware, and an owner decision on D5 is still pending, so do not advertise AMD Sealed.

#### Dedicated or shared: tell customers what your machine is
Set `host_kind` in `~/.computecoin/agent.toml` (Windows hosts always report `shared`):

- `host_kind = "dedicated"` — the machine does nothing but run ComputeCoin jobs (a server).
- `host_kind = "shared"` — you also use it (a laptop or gaming PC). Customers see a "Shared PC" label and a note that their job may run slower or be moved if you need the graphics card.
- Not set — customers see "unspecified", and a customer who asked for dedicated hosts only is never placed on your machine.

This is self-declared: say `dedicated` only if it is true. If a job is ended by the host (the machine goes to sleep, overheats, or is stopped), the platform re-runs it on another machine for free, the customer is not charged for the lost attempt, and the host earns nothing for it. Repeated losses count against the host's reputation.

#### Laptops and desktops you also use: step aside when you are busy (Linux)
Add `governance_enabled = true` to `~/.computecoin/agent.toml`, then start the agent with `computecoin-agent start`. It is **off by
default**, so a server keeps taking jobs exactly as before.

While it is on:

- **No new jobs** are taken while you use the computer (keyboard or mouse in the last 5 minutes), while another program is using
  the GPU (a game, a video editor), while you are on battery, or while GPU memory is short. The machine shows as "throttled" so
  customers' jobs go elsewhere.
- **A job already running is not killed.** It finishes. Its customer's work comes first.
- **`computecoin-agent pause`** stops hosting right now: the running job is stopped cleanly and re-run on another computer, the
  customer is not charged for it, and nothing new is taken until **`computecoin-agent resume`**.
- It goes back online only after the computer has been quiet for a minute (`governance_resume_clear_seconds`).

Settings (all optional): `governance_idle_threshold_seconds`, `governance_allow_on_battery`, `governance_vram_reserve_mb`,
`governance_schedule` (allowed hours), `governance_ignored_processes` (programs that never count as "you using the GPU"), and
`governance_headless = true` for a server with no desktop (keeps battery and schedule rules, ignores "is somebody at the keyboard").

What Linux cannot see, so it never blocks on it: a full-screen app and a pending reboot. GPU use by other programs is read for
NVIDIA cards only. Idle time comes from systemd-logind, so a desktop that does not report idle shows as "unknown", which on a
desktop means "be careful" (no new jobs). This was tested against sample tool output, not on a real laptop.

If another program was on the same GPU while a job ran, the agent tells the platform that (a yes and a count, never the program's
name) and the customer's job page says so. No signal means "not seen", not "alone on the card".

#### Windows hosts (planned, not available yet)
A Windows 10 or 11 PC with an NVIDIA card is meant to be able to host later: a native Windows service plus a managed WSL2 distro that runs the same Linux agent
(cross-platform plan W2 to W7). **Limits:** container tier only (no private tiers, BYOC, gVisor, microVM or multi-node jobs), NVIDIA only (AMD on Windows is a recorded
no-go), and the agent yields to the PC's owner by default. Nothing here has run on a Windows machine yet; the installer is written but has never been built, signed or run. Requirements, enrolment and troubleshooting:
[windows-host-guide.md](operations/windows-host-guide.md); plain-language website page: `/share/windows`. The Linux steps above do not change.

**Windows and the command line.** Every operator command in this guide and in [operator-cheatsheet.md](operations/operator-cheatsheet.md) has a PowerShell equivalent; the
support command `computecoin-agent support-bundle` writes a redacted diagnostics zip on either OS.

---

## 2.2 Provider overview — `/provider/dashboard`

Your fleet and your accrued earnings in one view. Check it to confirm machines
are **online** and earnings are moving.

---

## 2.3 Hardware — `/provider/hardware`

> *Capability is self-reported by the agent (Detected).*

Your enrolled machines: hostname, GPU, status, availability, price, region, last
heartbeat.

| Action | Effect |
|---|---|
| **Deregister** | Permanently removes the machine and invalidates its signing key |

**To appear in the marketplace** a machine must be online, available, priced, and
heartbeating. Miss any one and you get no work. If a machine vanishes from the
marketplace, check the heartbeat first — it is almost always a dead agent or a
network problem.

**If your machine dies mid-job**, the platform fails the job over to another
machine. The customer is charged exactly once. You are not paid for work you did
not complete.

---

## 2.3a Usage — `/provider/usage`

What your computer is doing **right now**, and how its time was used over the last
24 hours, 7 days or 14 days. Only your own computers are shown.

| Section | What it shows |
|---|---|
| **Right now** | For each graphics card: how busy it is, memory in use, temperature, power. Refreshes about every 10 seconds while the page is open |
| **Last hour** | How busy the graphics card was, minute by minute |
| **How your computer's time was used** | Running customer jobs / ready and waiting / not reporting |
| **What happened to the jobs** | Finished, did not finish, stopped on your computer, lost contact, ran out of time, cancelled |

**Where the numbers come from.** The Computecoin app on your computer reads the
graphics card about every 30 seconds and sends the readings about once a minute.
This is on by default; to turn it off set `telemetry_enabled` to `false` in the
app's settings (the page then says it has no readings).

**What the page will not do.**
- It never shows old numbers as live. With no reading in the last 10 minutes it says so.
- It never invents a split. If the computer sent no readings in the period, only
  the time spent on customer jobs is shown, with a note.
- It never shows customers' names, files or what a job was doing, and the app
  never sends the names of your own programs.
- "Ready and waiting" is an estimate from the readings, not a promise of earnings.

---

## 2.3b Keeping your hardware safe (Linux)

The Computecoin app protects your graphics card in layers. What each one does, and what it cannot do:

| Safeguard | What it does | Setting |
|---|---|---|
| **Heat limit** | At 85 °C it stops taking new jobs. At 95 °C it stops the running job. The customer is not charged and the job re-runs elsewhere | `gpu_temp_cap_c`, `gpu_temp_hard_stop_c` |
| **Lost sensor** | If the temperature reading worked and then goes silent for 2 minutes, it stops taking jobs and stops the running one. A computer that never had a reading is left alone | `gpu_sensor_lost_seconds` (10 to 3600) |
| **Power cap** | Runs NVIDIA cards at 90% of their own default power limit and puts the old limit back when stopped. It never raises a limit you set lower yourself. Needs root: the one-line installer runs a tiny separate root service for it, so the agent itself does not need root (without it, run the agent as root) | `gpu_power_limit_percent` (50 to 100; unset or 100 turns it off), or an exact `gpu_power_limit_w` |
| **Heat watchdog** | A separate small process. If the card stays over the heat limit (or the sensor stays silent) for 30 seconds longer than the app needs to react, it removes the customer containers. It covers a crashed or frozen app | `heat_watchdog_enabled` |
| **Clean start** | When the app starts it removes customer containers an earlier run left behind | always on |
| **No swap, no core dumps** | A job over its memory limit is stopped instead of pushing your own programs into swap, and a crashing job cannot write its memory to disk | always on |

**Run the watchdog as its own service.** The one-line install does this for you (it installs `computecoin-watchdog` as a separate service). If you installed by hand: the app starts the watchdog for you, but under systemd a process started by the app's own service is stopped with it. To keep it running when the app crashes, run `computecoin-agent watchdog --print-unit`, save the output as `/etc/systemd/system/computecoin-watchdog.service`, then `sudo systemctl enable --now computecoin-watchdog`.

**What this cannot do.** Software cannot promise a card never overheats. The card's own firmware still throttles and shuts down on its own, and that is the real last line of defence. If the app crashes and the watchdog is not running, a customer container can keep the card busy until the app is restarted, which clears it.

**Sandboxed jobs (optional).** Customers can ask for an extra sandbox (gVisor). A computer offers it only when gVisor is installed **and** registered with Docker (`sudo runsc install`, then restart Docker). The app says on start whether it is ready, and "My computers" shows "Sandbox ready" or "Ordinary jobs only". Support for graphics cards inside gVisor is still limited.

---

## 2.3c Container rental (opt-in) — `/provider/hardware`

> *Let customers run their own code on your graphics card.*

The same instructions are on screen for hosts: an optional step on the host setup page (`/provider/onboarding#container-rental`) and a box on "My computers". Customers get a "How renting a graphics card works" guide at the top of **Rent a GPU**, a card on the dashboard, a note on the public marketplace page, and entries in Help and the glossary.

Off unless you turn it on. First run `computecoin-agent rental check`: it tests Docker, the graphics card and a locked-down test container and says
what to fix. Then run `computecoin-agent rental on` on the computer (or install with `--allow-rental`), then restart the agent; `rental off`
turns it off and `rental status` shows the setting. A running rental is allowed to finish; no new ones start. "My computers" shows
"Container rental: on" after the next heartbeat, and customers can then find and pick that computer.

A stranger's code runs on your computer in a locked container: no extra rights, no internet unless the customer asked for a short list of package
sites, and only a private work folder it can write to, which is deleted at the end. Each rental is limited in disk (default 50 GB), memory and cores,
and is stopped if the card overheats, your disk runs low, you pause the agent, you start using the computer, or its processes look like a cryptocurrency
miner. While a rental runs the computer takes no normal job, and the other way round. A locked container is strong protection but not a perfect wall:
use a computer that holds nothing you could not afford to lose. Windows hosts cannot enable this.

---

## 2.4 Earnings — `/provider/earnings`

> *Live accrued earnings from the double-entry ledger as jobs settle.*

Earnings credit as each job completes — not when it starts, and not when it
fails. Every figure traces to a ledger entry you can inspect.

---

## 2.5 Payouts — `/provider/payouts`

| Action | How |
|---|---|
| **Request payout** | Enter an amount up to your available earnings |
| **Track payouts** | Status of each request |

**How payouts work.** You request; an administrator authorizes; the ledger moves
the money. You cannot overdraw — a request beyond your balance is rejected, and
this holds under concurrent requests.

**Current limitation:** real bank payouts via Stripe Connect are **not yet
live**. Payouts move simulated credits in the ledger. Real money movement needs
Stripe credentials to be configured.

---

# PART 3 — Admin guide

Operating the platform. All pages are administrator-only; customers and
providers receive 403.

---

## 3.1 Overview — `/admin/overview`

> *Platform-wide operational telemetry.*

Total users, active providers, job completion rate, recent activity. Your daily
first stop.

---

## 3.2 Users — `/admin/users`

| Action | Effect |
|---|---|
| **Suspend** | Account blocked immediately — their API calls start returning 401 |
| **Reinstate** | Restores access |
| **Confirm email** | Marks the account's email as confirmed by hand, for someone who cannot receive the confirmation email (for example while the platform's email sender is not yet set up for outside addresses). Needs a written reason of at least 5 characters, which is saved in the audit log |

You **cannot suspend yourself** — this is enforced, not a UI nicety. Every action
is audit-logged with actor, target, IP and outcome.

**Confirm email, with care.** It skips the proof that the person owns the mailbox, and a confirmed email unlocks billing, the wallet,
withdrawals, Stripe payout setup and running jobs. Use it only for people you know. It works at once (the person does not need to log in again),
asking again for an already confirmed account changes nothing, it is refused for deleted accounts, and it does not lift a suspension. It is available to
administrators only (an API key never carries this power). The proper fix is a verified sending domain in Resend; see
[operations/launch-checklist.md](operations/launch-checklist.md) and the deploy runbook.

---

## 3.3 Providers — `/admin/providers`

All registered machines visible in the marketplace, with hardware, pricing and
availability. Use it to see real supply.

---

## 3.4 All jobs — `/admin/jobs`

Platform-wide job list across every user: status, model, execution mode, final
cost, error codes. Use it to spot patterns — one machine failing everything, or
one customer burning budget.

---

## 3.5 Disputes — `/admin/disputes`

Open and resolved disputes. Each carries auto-attached evidence. Resolution
happens in Admin → Billing.

---

## 3.6 Billing & reconciliation — `/admin/billing`

> *Every money move is a balanced, audited ledger transaction.*

The most powerful page. Four tools:

| Tool | Use |
|---|---|
| **Reconciliation** | Proves total debits equal total credits. **Check this daily** — a non-zero imbalance means stop and investigate |
| **Refund** | Issue a refund against a job |
| **Authorize payout** | Approve a provider's payout request |
| **Resolve dispute** | Judge a dispute and refund if upheld |

**Guardrails you cannot bypass.** Refunds are capped at what was actually
captured, cumulatively — you cannot refund the same job twice past its total.
You cannot edit a balance directly; money only moves through a balanced, logged
transaction. Dispute resolution is idempotent and locks the row, so a double
click cannot double-refund.

---

## 3.7 On-chain settlement — `/admin/settlement`

Escrow locks, payout vouchers, and off-chain ↔ on-chain reconciliation.

| Action | What it does |
|---|---|
| **Issue voucher** | Signs a payout authorization with the platform key |
| **Confirm settlement / refund** | Records on-chain execution |
| **Reconciliation** | Compares our books against actual chain state |

**Read this before using it.** The reconciler reads the real contract over RPC
and reports **drift**, not "fine" — if it cannot read the chain it says so
rather than claiming agreement. Vouchers are signed at the escrow's live nonce,
carry an expiry, and cannot be replayed.

**Not production-ready:** contracts have not been through independent security
review, and no mainnet deployment should happen until they have.

---

## 3.8 Telemetry & fleet health — `/admin/telemetry`

Real-time GPU time-series: thermal, memory, power, and threshold alerts.

| Action | Effect |
|---|---|
| **Acknowledge alert** | Marks it seen |
| **Resolve alert** | Closes it |

Use it to catch a machine overheating or running out of memory before customers
notice.

Fleet metrics for operators (Prometheus, not this page) carry a `platform` label (`linux`, `windows`, `unknown` when a host reports nothing) and there are
Windows alert rules (runtime distro unhealthy, GPU smoke test failing, update rollbacks, hosts yielding): see [operator-cheatsheet.md](operations/operator-cheatsheet.md).
The page itself does not show the platform yet (fleet view is the W1 work package).

---

## 3.9 Third-party services — `/admin/vendors`

> *Usage figures show where they came from — a number counted by us is an
> estimate.*

A card per external service: Railway, Vercel, Supabase (database and storage),
Upstash, Resend, Sentry, the chain RPC provider, and Stripe. Each shows quota,
usage, plan, cost and renewal date.

**Two alarms**, each firing exactly once per period:

- **Usage threshold** — 50 / 80 / 100% of a quota
- **Renewal due** — a set number of days before a subscription renews

**Setting it up.** Open each card and enter its **cost** and **renewal date**.
These are left blank deliberately: they are facts about *your* account that
nobody has told the system, and inventing them would put fabricated financial
data on your dashboard. Once set, you get reminders before you are charged.

**How to read the cards honestly**

- **"Not measurable"** is not a failure and not a pass. Resend exposes no usage
  API at all. A green bar there would read fine right until signup breaks
  because nobody can be emailed.
- **Railway and Stripe show no percentage.** There is no ceiling to be a
  percentage of — Railway bills by compute-seconds and accrues continuously
  rather than hitting a wall. It is the service most likely to charge you with
  no warning.
- **The cost total is explicitly partial.** It states how many services have no
  cost set, so it is never mistaken for your whole bill.

---

## 3.10 Security posture — `/admin/security`

Current controls, planned improvements, roadmap. A reference page, not a live
feed.

---

## 3.11 Configuration — `/admin/configuration`

> *Read-only view of active platform settings. Changes require a deployment.*

Shows the live configuration derived from environment variables. Read-only by
design — configuration is not editable from a web page.

---

## 3.12 Audit logs — `/admin/audit-logs`

> *Immutable record of sensitive administrative actions.*

Every entry: actor, action, target, IP, outcome, timestamp. Suspensions,
refunds, payout authorizations, dispute resolutions, vendor changes.

Append-only. An administrator cannot quietly alter history.

---

# Features available via API but not yet in the UI

These are built, tested and reachable through the API, but have **no console
page yet**. Listed so you know they exist rather than assuming they are missing.

| Feature | Endpoint |
|---|---|
| Spend limits with threshold alerts | `/v1/spend-limits` |
| Webhooks (subscribe to events) | `/v1/webhooks/endpoints` |
| Provider reputation scores | `/v1/reputation` |
| Batch job submission | `/v1/batches` |
| Datasets and training checkpoints | `/v1/datasets`, `/v1/training` |
| Organizations and team invitations | `/orgs` |
| Enterprise SSO (OIDC) | `/sso` |
| Provider pricing rules and availability windows | `/v1/provider/machines/{id}/pricing` |
| Usage analytics and CSV export | `/v1/analytics` |
| Chain reconciliation runs | `/v1/admin/chain-recon` |
| Stripe Connect payouts | `/v1/provider/connect` |
| Real-time job push (WebSocket) | `/v1/ws` |

SAML SSO is **not** implemented. OIDC is.

---

# Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Cannot submit a job | Email not verified | Verify your email |
| `model_not_supported` | Used a model *name* instead of its **version tag** | Use e.g. `echo-sim-v1` |
| `invalid_input` | Wrong input shape | `prompt` for text-generation, `items` for batch-inference |
| `insufficient_credits` | Balance too low for the hold | Buy credits |
| Machine not in marketplace | No recent heartbeat, or not priced/available | Check the agent is running |
| Job stuck in `queued` | No machine matches your requirements | Lower GPU count, or clear the target machine |
| Lost API key | Only a hash is stored | Revoke it, create a new one |
| Lost machine secret | Shown once only | Deregister the machine and re-enrol |
| No verification emails | Email quota exhausted | Check Admin → Third-party services |
| Artifact link expired | Download grants are short-lived | Reopen the job and generate a fresh link |

---

# Security model, briefly

What protects you, and what is verified by test rather than asserted:

- **Passwords** — Argon2id hashed; plaintext never stored
- **Sessions** — httpOnly cookies, CSRF double-submit, short access tokens with
  rotating refresh tokens and reuse detection
- **Agents** — every request HMAC-signed; forged signatures and tampered bodies
  rejected
- **API keys and machine secrets** — shown once, stored hashed
- **Job execution** — hardened containers: no Docker socket, no host mounts, all
  capabilities dropped, network disabled, resource limits enforced
- **Artifact access** — short-lived signed grants, scope-bound and
  tamper-evident; extending an expiry or re-pointing a grant is rejected
- **Isolation** — one tenant cannot read another's jobs, invoices or disputes;
  strangers receive 404, so existence is not disclosed
- **Money** — double-entry ledger; no negative balances; no GPU double-booking
  under concurrency; refunds capped cumulatively at what was captured

---

# Before you rely on this in production

Honest status, so nobody is surprised:

1. **No GPU has executed a job yet.** The code exists; it has not run on real
   hardware. Everything is `simulated`.
2. **Real payments are not live.** Stripe is wired but has never run against
   real credentials — customers cannot truly pay, providers cannot truly be paid.
3. **No independent security or legal review** has been done. Both are required
   before mainnet contracts or a public token.

The foundation is solid — the ledger, isolation, auth and scheduling are tested
and proven under concurrency. It is the last mile that remains.
