# ComputeCoin — Execution Plan: Phases A → D

_Status date: 2026-10-22 · Owner: engineering · Companion to
[PROJECT_REVIEW_AND_ROADMAP.md](PROJECT_REVIEW_AND_ROADMAP.md),
[AUDIT_REPORT.md](../../AUDIT_REPORT.md) and
[VERIFICATION_PLAYBOOK.md](VERIFICATION_PLAYBOOK.md)._

This document defines, per phase, **what to build (Do)** and **what to avoid
(Don't)** at a level an engineer can execute against. Every phase lists its
acceptance criteria and marks each item as **[code]** (buildable + testable now,
no GPU), **[code+ext]** (code buildable now, needs an external account/hardware
to fully run), or **[external]** (not code — a third party must do it).

## Global guardrails (apply to every phase)
**Do**
- Keep the **honesty stamp**: every inference response carries `execution_mode`
  (`simulated` | `hardware`). Simulated output is never presented as real.
- Route **every money movement** through the single `post_transaction` ledger
  choke point (balanced, idempotent, no-negative). Add tests that assert the
  invariant holds.
- Make every externally-triggered action **idempotent** (job submit, webhook
  delivery, payment capture, settlement confirm) keyed by a stable id.
- Enforce **tenant isolation** on every new endpoint (owner-or-admin, 404 to
  strangers) and add an IDOR test for it.
- Ship each change with a **proving test** and keep the suite green.

**Don't**
- Don't mark simulated work as `hardware`, fabricate metrics, or bill a
  failed/cancelled job as a success.
- Don't bypass the ledger or mutate balances directly.
- Don't add an endpoint that reads/writes a resource by id without an owner
  filter.
- Don't take user-supplied URLs (webhooks, callbacks, model sources) without an
  **SSRF allowlist** and blocking of internal ranges.
- Don't deploy contracts to mainnet, or launch a tradable token, without an
  **external audit + legal review**.

---

# Phase A — "Make it real" (prerequisite for B/C/D)

**Goal:** a real model runs on a real GPU, streamed through the public API, billed
on measured tokens. Until this exists, B/C/D can only be tested against the
simulator.

### A1. Agent model-runtime layer **[code+ext]**
- **Do:** add a runtime abstraction in the provider agent that launches an
  inference server (**vLLM** first; **llama.cpp** for CPU/low-VRAM) **inside the
  existing hardened container profile** (no host mounts, no docker socket,
  `--gpus`, resource limits). Pin model images by digest in the model registry
  with VRAM requirements so the scheduler can match on them.
- **Don't:** don't run the model in the agent process; don't pull arbitrary
  user-specified images; don't skip the isolation flags "to make it work".

### A2. Streaming inference contract **[code]**
- **Do:** implement **SSE** on `POST /v1/chat/completions` when `stream=true`,
  with token-level chunks, a correct final `usage` chunk, a `[DONE]` sentinel,
  cancellation, and timeouts. Define the agent↔API streaming envelope.
- **Don't:** don't keep rejecting `stream=true` (it's the default for most
  OpenAI-compatible clients); don't buffer the whole response and fake streaming.

### A3. Real token metering **[code+ext]**
- **Do:** bill **prompt/completion tokens as reported by the runtime**,
  cross-checked by the API; keep `_estimate_tokens` only as a fallback/sanity
  bound. Keep the disclosed minimum-billable policy.
- **Don't:** don't charge estimated tokens when real counts are available; don't
  let the provider's self-reported count be the sole source of truth.

**Acceptance:** one real model, on one real GPU, answers a **streamed** request
through the public API, and the ledger charges the **measured** token count.

---

# Phase B — "Make it trustworthy"

**Goal:** a cheating or failing provider cannot get paid for bad work, and jobs
survive provider crashes. **~100% code-buildable now** (testable against the
simulator; real-workload value lands with Phase A).

### B1. Failover & retry with idempotent billing **[code]**
- **Do:** when a provider drops mid-job (lease expiry), **reschedule on another
  machine**; reuse the per-job idempotency keys so the customer is **never
  double-charged** and capacity is always released. Cap retries; record each
  attempt in `job_attempts`.
- **Don't:** don't create a second hold/charge on retry; don't leave the old
  reservation active; don't retry a job the customer cancelled.
- **Acceptance:** a job completes after its first provider is killed, with exactly
  one charge.

### B2. Result verification **[code]** (value realized with Phase A)
- **Do:**
  - **Known-answer challenges:** randomly inject jobs with a fixed prompt
    (temperature 0, fixed seed) whose expected output is known; a mismatch lowers
    reputation. Make challenge jobs indistinguishable from real ones to the
    provider.
  - **Redundant execution:** re-run a **small configurable sample (1–5%)** of paid
    jobs on a second provider; compare (exact for greedy decoding,
    embedding-distance/logprob tolerance otherwise).
  - **Model-identity:** agent reports a **weight hash**; validate it against the
    registry.
- **Don't:** don't trust a container exit code as proof of work; don't run
  redundant execution on 100% of jobs (cost explosion); don't make challenge jobs
  obviously different (a cheater would detect and only cheat real ones); don't pay
  out before the verification window where configured.
- **Acceptance:** in a staged test, a provider returning wrong output is detected
  and penalized.

### B3. Reputation & slashing **[code]**
- **Do:** feed completion rate, timeouts, verification failures, disputes and
  uptime into a **time-decayed reputation score** (the Bayesian reliability score
  added in `scheduler.py` is the seed). On **proven** fraud, withhold or claw back
  earnings via explicit, audited ledger transactions; surface history in the
  provider console.
- **Don't:** don't slash on a single ambiguous signal; don't claw back silently
  (every adjustment is a logged ledger entry with a reason); don't let reputation
  be computed from provider-self-reported numbers.

### B4. Dispute workflow with evidence **[code]**
- **Do:** let a customer dispute a settled job; attach **evidence** (inputs,
  outputs, hashes, challenge/redundancy results); give admins a one-screen resolve
  flow (refund/penalty) that writes an audit trail and balanced ledger entries.
- **Don't:** don't allow disputes to mutate the ledger outside the choke point;
  don't expose one party's private inputs to the other.

### B5. Spend limits, usage analytics, webhooks **[code]**
- **Do:** hard **spend caps** per project/API key with alerts at 50/80/100%;
  **usage analytics** (per project/key/model, CSV export); **webhooks** for job
  completed/failed, low balance, invoice paid — signed, retried with backoff,
  idempotent on delivery id.
- **Don't:** don't deliver webhooks to user URLs without an **SSRF allowlist**
  (block `127.*`, `10.*`, `169.254.*`, metadata endpoints); don't send unsigned
  webhooks; don't let a spend cap be bypassed by concurrent requests (enforce
  inside the locked ledger path).

### B6. Operational plumbing **[code]**
- **Do:** **require Redis** for rate limiting when `ENVIRONMENT != development`
  (the degradation warning added this session becomes an enforced requirement);
  move provider connectivity from 5s polling to an **outbound WebSocket/gRPC
  stream** so jobs reach GPUs instantly and NAT'd providers work without inbound
  ports.
- **Don't:** don't fail the whole API closed on a transient Redis blip (degrade to
  a strict low limit instead); don't require providers to open inbound firewall
  ports.

**Phase B exit:** a cheating provider is caught and penalized **and** a job
completes after a provider crash — both in a staged, automated test.

---

# Phase C — "Make it grow"

**Goal:** strangers self-serve without hand-holding. Mostly **[code]**; a few
items need your accounts.

### C1. Batch API **[code]**
- **Do:** accept many requests, process asynchronously, return results with a
  discount; reuse the job FSM + ledger.
- **Don't:** don't lose idempotency across a batch; don't bill partial batches as
  complete.

### C2. Teams / organizations + SSO **[code]** / SSO **[code+ext]**
- **Do:** shared projects, roles, invitations (the `organizations` /
  `organization_members` tables already exist); add **OIDC/SAML SSO**.
- **Don't:** don't let an org member escalate to owner; **SSO end-to-end needs a
  real IdP account to validate** — build the code, test with a mock IdP, confirm
  later.

### C3. Fine-tuning / training jobs **[code]** (+ **[external]** to run)
- **Do:** dataset upload (signed URLs, size/type limits, malware scan hook),
  checkpoint metadata, resume, artifact download — all as job types.
- **Don't:** *running real training needs a GPU* — ship the plumbing now; the
  actual training run is GPU-gated.

### C4. Provider onboarding & payouts **[code]** / payouts **[code+ext]**
- **Do:** one-line installer + auto-benchmark; pricing controls (floor price,
  availability windows, model allow-list); **Stripe Connect** payouts with history
  and tax docs.
- **Don't:** **Stripe Connect needs a real Stripe account/entity to run** — build
  and test against Stripe test mode; don't pay out real money without KYC.

### C5. SDK publishing & scale **[code+ext]**
- **Do:** polish SDKs (retries, streaming helpers, typed errors, examples);
  publish to **PyPI/npm**; record load-test baselines; write an autoscaling plan.
- **Don't:** **publishing needs PyPI/npm tokens**; never ship a token/secret in a
  published package; don't declare scale-ready without a real load test.

**Phase C exit:** external beta providers and customers onboard themselves.

---

# Phase D — "Make it on-chain" (last; token phase)

**Goal:** a real token/settlement rail — **only after demand is proven**. Contract
work is **[code]**; audit and legal are **[external]** and are hard gates.

### D1. Contract fixes & tests **[code]**
- **Do:** finish remaining items (jobId key **done**; verify voucher binds chain
  id + contract address + **nonce + expiry** and test cross-chain/contract
  replay); add **invariant/fuzz tests** (Foundry/Echidna): `sum(locks) ==
  sum(payouts + refunds + fees)`.
- **Don't:** don't add admin powers that can seize user funds; don't ship
  `unchecked` math; don't let pause freeze user refunds.

### D2. Real on-chain ↔ ledger reconciliation **[code+ext]**
- **Do:** a reconciler that reads on-chain escrow state/events via RPC and
  compares to `OnChainEscrow` + the off-chain ledger; alert on drift. (Full run
  needs a **testnet RPC**.)
- **Don't:** don't report "reconciled" from an internal flag (already fixed —
  keep it a real comparison).

### D3. Deploy path **[code+ext]** → **[external]**
- **Do:** testnet deploy scripts + a multisig/timelock owner; testnet soak;
  staged, capped mainnet.
- **Don't (hard rules):**
  - **Don't deploy to mainnet or open a real liquidity pool without an external
    audit + legal review.**
  - Don't launch a "temporary" mainnet coin — it is permanent and abandoning it is
    a rugpull.
  - Don't seed a mainnet pool with trivial liquidity (it gets drained).
  - Never market the token as a guaranteed-return investment; never do wash
    trading / fake volume / hidden allocations.

**Phase D exit:** audited contracts, real reconciliation green on testnet, a
documented capped-mainnet plan — executed **only** after A–C and sign-off.

---

## What is NOT code (cannot be delivered by building, in any phase)
- **External smart-contract audit** (Phase D) — third-party firm, weeks + budget.
- **Legal / regulatory review** (Phase D payouts + token) — counsel.
- **Running real GPU inference/training** (A, B-value, C3) — real hardware.
- **Third-party accounts/entity**: Stripe (+ Connect), an SSO IdP, PyPI/npm
  tokens, cloud hosting + domain, production load infra.

## Honest status of "build it all in code now"
- **Phase B:** fully code-buildable and testable now (against the simulator).
- **Phase C:** mostly code now; SSO/Stripe-Connect/publishing/scale need accounts
  to *run*.
- **Phase D:** contracts + tests + reconciler code now; **audit + legal + mainnet
  are hard external gates.**
- **Caveat on all of it:** code built before Phase A is validated against
  **simulated** output — "code-complete and test-green", **not** "proven on real
  workloads". That proof waits on a GPU.

## Recommended order
**A (real inference) → B (trust) → C (growth) → D (on-chain).** If building code
ahead of hardware: **B first** (highest value, fully testable), then the
account-independent slices of C, then D's contract work — each shipped green with
the honest "validated on simulator" label until a GPU and an audit are available.
