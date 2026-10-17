# ComputeCoin — How It All Works (The Full Story, Admin → User)

_A simple, story-style walkthrough of the whole platform: every actor, every
feature, in the order they actually happen. No jargon where plain words will do._

---

## The cast (who's who)
- **Developer / Customer** — needs GPUs to run AI jobs. Pays with credits.
- **Provider** — owns a GPU, rents it out, earns money.
- **Admin / Operator** — runs the platform: handles money, disputes, safety.
- **The Agent** — a small program the provider installs on their GPU machine.
- **Behind the scenes** — the scheduler, the ledger, the recovery sweeper: the
  "staff" nobody sees but everyone relies on.

Everyone shares one website and one API. Who you are (your **role**) decides what
you're allowed to do — and that's enforced on the server, not just hidden in the UI.

---

## ACT 1 — A developer arrives and sets up

1. **Sign up & verify.** They register with email + password (hashed with Argon2id,
   never stored in plain text), verify their email, and log in. On the website,
   login stores the session in **secure httpOnly cookies** (safe from JS/XSS); the
   SDK/CLI use a normal API token instead. *(Pages: `/register`, `/login`,
   `/dashboard`.)*
2. **Create a project.** Work is organized into projects. *(`/dashboard/projects`.)*
3. **Make an API key.** For calling ComputeCoin from code. The full key is shown
   **once**; the server only keeps a hashed copy. *(`/dashboard/api-keys`.)*
4. **Get credits.** They buy compute credits (real money later via Stripe/Razorpay;
   today these are clearly-labelled test credits). Balance shows **available** vs
   **reserved**. *(`/dashboard/billing`.)*

Now they're ready to rent GPUs.

---

## ACT 2 — A provider plugs in their GPU

1. **Become a provider.** A user flips on the provider role and does onboarding.
   *(`/provider/onboarding`.)*
2. **Generate an enrollment token.** A one-time, expiring code that links a machine
   to their account. Shown once.
3. **Install the Agent** on the GPU machine and run:
   `computecoin-agent enroll --token <code> --server <url>` then
   `computecoin-agent run`.
4. **The Agent detects the real hardware** (GPU model, VRAM, CPU, RAM) and registers
   the machine. It only reports what it actually finds — it can label specs
   **"Detected,"** never fakes **"Verified."**
5. **Signed heartbeats.** The Agent keeps sending signed "I'm alive + here's my
   health" messages, so the platform knows the machine is online. *(It signs each
   message so nobody can impersonate the machine.)*
6. **Set price & availability.** The provider sets an hourly price, supported job
   types, and online/maintenance state. *(`/provider/hardware`,
   `/provider/dashboard`.)*

Now a real, online, priced GPU exists in the marketplace.

---

## ACT 3 — The developer finds and books a GPU

1. **Search the marketplace.** Filter by GPU model, VRAM, price, region, workload.
   Only **online, available, priced** machines show up — no fake inventory. Ratings
   show "No history yet" honestly until real jobs exist. *(`/marketplace/gpus`.)*
2. **Get a quote.** Pick a machine + job details → an **itemized** estimate (GPU
   cost + platform fee + total) with a **countdown expiry**. The fee is shown
   **before** you commit. *(Quote dialog.)*
3. **Reserve the slot.** Converting the quote to a reservation **atomically** locks
   capacity. If two people grab the last slot at the same moment, **exactly one
   wins** — the other gets a clean "no capacity." (Proven under real concurrency.)

---

## ACT 4 — A job runs (the heart of it)

A job moves through a strict **finite-state machine** — it can only take legal
steps, and every step is recorded forever:

```
CREATED → VALIDATING → QUEUED → MATCHED → RESERVED → RUNNING → VERIFYING → COMPLETED
                 (or FAILED / CANCELLED / TIMED_OUT / RETRY_PENDING along the way)
```

1. **Submit.** Developer submits a job (text-generation or batch-inference) via the
   UI, API, or SDK — with an **idempotency key** so a double-click never creates two
   jobs. *(`/dashboard/jobs`, `POST /v1/jobs`, or OpenAI-compatible
   `/v1/chat/completions`.)*
2. **Validate.** Model approved? Inputs valid? GPU count OK? Enough credits? If not,
   it fails cleanly (e.g. `402 insufficient_credits`).
3. **Hold the money.** Credits for the estimate are **held** (moved to reserved) so
   you can't overspend.
4. **Schedule & reserve.** The scheduler scores eligible machines (price, health,
   reliability) and picks one, then atomically reserves it — **no double-booking.**
5. **The Agent claims it** (exactly once) and starts running — in an **isolated**
   environment (hardened container: no host access, no docker socket, resource
   limits). On CPU-only infra it runs a clearly-labelled **simulated** executor; on
   a real GPU it runs the real model.
6. **Live updates.** The Agent streams **logs**, sends **heartbeats** (which extend
   its lease), and uploads **output artifacts** via short-lived signed links.
   *(Job detail page shows a live timeline, logs, and downloads.)*
7. **Verify & complete.** The platform checks the output shape (not just "exit 0"),
   records the result, and marks it **COMPLETED**. Artifacts are private to the
   owner.
8. **If the provider vanishes mid-job:** a background **recovery sweeper** notices
   the expired lease, frees the slot, returns the held credits, and requeues or
   times out the job — **a job never gets stuck forever.**

---

## ACT 5 — The money moves (and always balances)

Every money movement is a **double-entry ledger** transaction (debits always equal
credits — the books can never silently go wrong):

- **On success:** the hold is **captured** → split into **provider earnings** +
  **platform fee**; any unused hold is **released** back to the customer. An
  **itemized invoice** is created (estimated vs actual). *(`/dashboard/invoices`,
  `/dashboard/usage`.)*
- **On failure/cancel/timeout:** the hold is **fully released** — a failed job is
  **never** charged as a success.
- **Provider payouts:** providers see **live earnings** and request payouts; the
  ledger refuses to pay out more than they've earned. *(`/provider/earnings`,
  `/provider/payouts`.)*
- **Disputes & refunds:** a customer can open a **dispute** on a settled job; an
  admin can issue a **refund** — capped so you can never refund more than was
  charged, and the ledger stays balanced. *(`/dashboard/disputes`; admin side.)*

---

## ACT 6 — Watching the whole fleet (telemetry)

The Agent streams performance telemetry (GPU utilization, memory, tokens/sec) for
jobs it actually runs — and a machine **cannot** inject telemetry for someone
else's job. Operators get a **fleet dashboard** (active GPUs, utilization, power)
and **alerts** (e.g. overheating, repeated disconnects) they can acknowledge and
resolve. *(Telemetry endpoints + fleet/alerts views.)*

---

## ACT 7 — The coin & on-chain settlement (testnet for now)

For providers/customers who want on-chain settlement, there's an **escrow** flow:
a customer locks budget in an escrow (keyed to *their* job so no one can squat it),
the platform issues a **cryptographically signed voucher** authorizing the payout,
and the on-chain contract releases funds to the provider (or refunds the customer
after a deadline). This runs on a **test network today**; a real-money/mainnet coin
is a later step gated on audit + legal. The off-chain ledger and on-chain escrow
are **reconciled** so they can't silently drift. *(`/v1/settlement/*`,
`ComputeSettlement.sol`.)*

---

## ACT 8 — The admin's control room

Admins (and support/security operators) have powers everyone else doesn't — and
**every sensitive action is logged** with who/when/why:

- **Users & providers:** view, **suspend**, reinstate. *(`/admin/users`.)*
- **Money oversight:** **reconciliation** panel proving the ledger balances
  (imbalance = 0.00), **authorize payouts**, **issue refunds**, **resolve
  disputes** — all admin-only, enforced on the server (RBAC). *(`/admin/billing`,
  `/admin/payments`.)*
- **Investigate anything:** **audit logs** of every login, refund, suspension,
  settlement. *(`/admin/audit-logs`.)*
- **Platform settings:** fees, supported models, feature flags. *(`/admin/configuration`.)*

Admins **cannot** secretly edit balances — the only way money moves is a balanced,
logged ledger transaction.

---

## The quiet guarantees (why it's trustworthy)
- **No double-booking** — the same GPU slot can't go to two jobs (proven under real
  concurrency on Postgres).
- **No double-charge / no negative balance** — the ledger refuses it, even under
  simultaneous requests.
- **The books always balance** — debits == credits, always.
- **A failed job is never billed as success.**
- **A disconnected provider never strands a job** — the sweeper recovers it.
- **Your data is yours** — one customer can never see another's jobs, logs,
  artifacts, invoices, or escrows.
- **Honesty** — demo/simulated data is always labelled; no fake benchmarks, no fake
  token metrics.

---

## Feature → where it lives (quick map)
| Feature | Page / API |
|---|---|
| Auth, projects, API keys | `/register` `/login` `/dashboard/projects` `/dashboard/api-keys` |
| Provider onboarding + agent | `/provider/onboarding` `/provider/hardware` + `computecoin-agent` |
| Marketplace + quote + reserve | `/marketplace/gpus` + quotes/reservations API |
| Jobs (submit/track/logs/artifacts) | `/dashboard/jobs` `/v1/jobs` `/v1/chat/completions` |
| Billing / invoices / usage | `/dashboard/billing` `/dashboard/invoices` `/dashboard/usage` |
| Earnings / payouts / disputes | `/provider/earnings` `/provider/payouts` `/dashboard/disputes` |
| Telemetry / fleet / alerts | telemetry endpoints + fleet dashboard |
| On-chain settlement (testnet) | `/v1/settlement/*` + `ComputeSettlement.sol` |
| Admin control room | `/admin/*` |

---

## The Winning Playbook — Product Launch vs Token Launch (Read This)

People often confuse **launching the product** with **launching the coin**. They happen at two different times for very good reasons:

### Phase 1: Launch the Product (Now — Right After D & E)
- **What is live:** The website (`computecoin.xyz` on Vercel), backend (Railway), database (Supabase), and Stripe billing.
- **The Token:** Stays on **Testnet (Sepolia)** — costs ₹0, zero legal risk, verifiable on Etherscan, and fully functional for demos and testing.
- **The Money:** Real customers buy compute credits via **Stripe**. Real providers earn real money for renting out GPUs.
- **The Result:** You have a real, revenue-generating software business.

### Phase 2: Launch the Coin on Mainnet (Later — When Revenue Pays For It)
Launching a real tradable coin with real money on exchanges has 3 non-negotiable legal and security requirements:
1. **Security Audit (₹8L+ / $10k+):** On a blockchain, smart contract code is permanent. An outside audit firm must verify the contract before real money is deposited, otherwise bots or exploits drain user funds.
2. **Legal Clearance:** A quick legal review ensuring compliance with local crypto regulations (e.g. 30% VDA tax in India + 1% TDS, securities laws).
3. **Real Liquidity (₹50k–₹5L+):** On testnet, faucet coins create the liquidity pool for free. On mainnet, you need real ETH/USDT paired with tokens so trading bots don't drain the pool.

### The Playbook Flow:
```
       AFTER D & E (NOW)
               │
               ▼
 ┌───────────────────────────────┐
 │ 🌍 Launch the Web Platform   │  <-- Global website is LIVE
 │ 🪙 Keep Token on TESTNET     │  <-- Real explorer link, ₹0 risk, free swaps
 │ 💳 Accept Real Stripe Cash   │  <-- Real revenue from real customers
 └──────────────┬────────────────┘
                │
                ▼ (Earn revenue / get first 50–500 users)
 ┌───────────────────────────────┐
 │ 💰 Pay for Security Audit     │  <-- Paid with product revenue/investors
 │ ⚖️ Get Quick Legal Clearance │
 └──────────────┬────────────────┘
                │
                ▼ (Safe, Legal, High Value)
 ┌───────────────────────────────┐
 │ 🚀 MAINNET TOKEN LAUNCH       │  <-- Token price actually goes UP because
 │ 💎 Airdrop to Real Users      │      you have real users & real revenue!
 └───────────────────────────────┘
```

**Rule of thumb:** Users first. Revenue second. Mainnet coin third. That's how real, lasting projects are built.

---

## One-paragraph summary
A **developer** signs up, buys credits, and submits an AI job. A **provider** has
plugged in a real GPU via the Agent. The **marketplace** matches them, **reserves**
the GPU without double-booking, runs the job **safely in isolation**, streams logs
and results, and settles the money through a **double-entry ledger that always
balances** — provider earns, platform takes a fee, customer gets an itemized
invoice. If anything breaks, the job is **recovered** and money **returned**.
**Admins** oversee it all through a logged control room, and an optional **on-chain
settlement** (on testnet today) mirrors the money for those who want it. Every
promise above is backed by tests.

_See [business2.md](business2.md) for the launch path, [LAUNCH_PLAN.md](LAUNCH_PLAN.md)
for remaining work, and [AUDIT_REPORT.md](AUDIT_REPORT.md) for security status._
