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

**How to participate.** Use the key as a bearer token against the API, or point
an OpenAI-compatible client at the inference endpoint. Scope keys per project so
revoking one does not break everything.

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

Install the agent on the GPU host and give it the enrollment token. The agent:

1. Detects your real hardware (GPU model, VRAM, CPU, RAM, disk)
2. Enrols with the token
3. Receives a **machine secret** — shown once, used to sign every request
4. Begins sending signed heartbeats

Every agent request is HMAC-signed. A forged signature or a tampered body is
rejected with 401 — verified by test.

**Requirements:** Docker, and the NVIDIA container toolkit for GPU jobs.

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

You **cannot suspend yourself** — this is enforced, not a UI nicety. Every action
is audit-logged with actor, target, IP and outcome.

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
