# Phase 5 — Billing, double-entry ledger, invoices, refunds, disputes (AUTHORITATIVE CONTRACT)

Single source of truth for Phase 5. Backend (`apps/api`), web (`apps/web`), SDKs
(`sdk/*`) MUST conform exactly. Builds on Phase 3 (pricing/quotes) and Phase 4
(job lifecycle). This is the MONEY layer — correctness over features.

## Money representation (NEVER float)
- Internally the ledger stores **integer minor units** (`amount_minor: BigInteger`,
  always > 0 per entry) with a `currency` and fixed `exponent` (2).
- Currency for the MVP is `"USDC_CREDITS"` — **simulated test credits**, clearly
  labelled in the UI; no real payment rail. `PAYMENT_PROVIDER_CONFIGURATION=simulated`.
- The API exposes amounts as **decimal strings** ("12.34"); convert at the edge.
- All arithmetic uses integers (minor units) or `Decimal` — never `float`.

## Double-entry ledger (THE core invariant)
- `ledger_transactions` (id, kind, reference_type, reference_id, idempotency_key
  UNIQUE, status, created_at). kinds: `credit_purchase`, `hold`, `capture`,
  `release`, `payout_request`, `payout_settle`, `refund`, `adjustment`.
- `ledger_entries` (id, transaction_id FK, account_id FK, direction[`debit`|`credit`],
  amount_minor BigInteger>0, currency, created_at). IMMUTABLE (append-only).
- **INVARIANT 1 — every transaction balances:** within a transaction,
  SUM(debit amount_minor) == SUM(credit amount_minor). The posting function
  asserts this and refuses to write an unbalanced transaction.
- **INVARIANT 2 — conservation:** across the whole ledger, SUM(all debits) ==
  SUM(all credits). (A reconciliation query must return 0 imbalance.)
- `billing_accounts` (id, owner_type[`customer`|`provider`|`platform`|`external`|`holding`],
  owner_id?, type, currency, balance_minor BigInteger cached, created_at). The
  cached balance is updated INSIDE the same DB transaction as the entries and must
  always equal the derived SUM(credits)-SUM(debits) for that account's normal side.
  Account normal sides: customer_credits & provider_earnings & platform_revenue are
  CREDIT-normal (balance = credits - debits); external & holding are internal.

### Posting primitive (single choke point)
`post_transaction(kind, legs=[(account, direction, amount_minor), ...],
idempotency_key, reference)`:
1. assert SUM(debits)==SUM(credits) (INVARIANT 1) else refuse;
2. idempotent: if idempotency_key already posted, return the existing transaction
   (NO second effect) — this is how double-charge is prevented;
3. in ONE DB transaction (row-lock the affected accounts with `with_for_update`):
   insert the transaction + entries, update each account's cached balance, and
   REFUSE if it would drive a customer_credits/holding balance negative
   (INVARIANT 3 — no negative balances, even under concurrency).

## Signup grant (so flows work in dev/test)
On first billing interaction a customer's `customer_credits` account is created
and granted `BILLING_SIGNUP_GRANT_MINOR` (config; dev/test default large, e.g.
100000 == "1000.00") via a `credit_purchase` from the `external` funding account.
Clearly labelled as simulated promotional credits. This keeps Phase 1-4 flows
funded without weakening their tests.

## Job lifecycle integration (hooks into Phase 4 — keep Phase 4 tests green)
- **On submit (after VALIDATING, BEFORE reserving machine capacity):** HOLD the
  authorized amount (the quote total, capped at `max_cost`) from the customer:
  `hold` txn = debit customer_credits, credit holding. Insufficient balance -> the
  submit fails with **402 insufficient_credits** and the job -> FAILED
  (no machine reserved). This is budget protection + no-overspend.
- **On COMPLETED (settle):** meter actual usage -> `actual_cost` = price_per_hour *
  actual_hours (from job metrics) + platform_fee, **capped at the held amount**.
  `capture` txn = debit holding (actual_cost) split into credit provider_earnings
  (compute portion) + credit platform_revenue (fee); then `release` the remainder
  of the hold back to customer_credits. Idempotent on job_id (NO double charge).
  Writes a `usage_records` row (estimated vs actual kept separate) + an `invoices` row.
- **On FAILED/CANCELLED/TIMED_OUT (release):** `release` txn returns the ENTIRE hold
  to customer_credits. **A failed job is never charged.** No invoice (or a zero
  invoice marked not-charged).
All three are idempotent and always leave the ledger balanced.

## New DB tables
`billing_accounts`, `ledger_transactions`, `ledger_entries`, `usage_records`,
`invoices`, `payments`, `refunds`, `provider_payouts`, `disputes`. Portable types;
money as BigInteger minor units; Numeric only if you keep decimals (prefer integer).

## Endpoints

### Customer (JWT)
- `GET /v1/billing/account` -> { currency, available: "998.35", reserved: "1.65", balance: "1000.00" }
  where `available` = spendable (customer_credits balance), `reserved` = current
  holds (holding attributable to the customer), and `balance` = available + reserved
  (total owned). (Clarification: an earlier draft swapped the `available`/`balance`
  example values; `available` is always the spendable figure and is what drops when
  a hold is placed.)
- `POST /v1/billing/credits/purchase` { "amount": "50.00" } -> simulated top-up
  (credit_purchase txn: debit external, credit customer_credits). Returns new account.
- `GET /v1/billing/ledger` -> the customer's ledger entries (own only), paginated,
  each { entry_id, transaction_id, kind, direction, amount, currency, reference, created_at }.
- `GET /v1/invoices` / `GET /v1/invoices/{id}` -> itemized invoice per settled job
  { invoice_id, job_id, line_items:[{label, amount}], estimated_total, actual_total,
    platform_fee, status, created_at }. `GET /v1/invoices/{id}/download` -> signed URL or inline.
- `GET /v1/usage` -> spend summary + usage_records (per job: duration, estimated vs actual cost).
- `POST /v1/jobs/{job_id}/dispute` { reason } -> opens a dispute (status open) on a settled job.
- `GET /v1/disputes` / `GET /v1/disputes/{id}` (own).

### Provider (JWT, provider role)
- `GET /v1/provider/earnings` -> LIVE accrued earnings from the ledger
  { available: "...", lifetime: "...", currency, recent:[...] } (replaces the demo).
- `POST /v1/provider/payouts` { amount } -> request a payout (payout_request txn:
  debit provider_earnings, credit payout_pending holding). 409 insufficient_earnings
  if amount > available. Returns the payout (status requested).
- `GET /v1/provider/payouts` (own).

### Admin / operator (JWT, administrator or support/security operator as appropriate; MFA assumed)
- `POST /v1/admin/refunds` { job_id | invoice_id, amount, reason } -> authorize a
  refund (refund txn REVERSES the charge: debit platform_revenue + provider_earnings
  proportionally, credit customer_credits). Explicit, audited, least-privilege.
- `POST /v1/admin/payouts/{id}/authorize` -> settle a payout (payout_settle:
  debit payout_pending, credit external) — authorized role only, audited.
- `POST /v1/admin/disputes/{id}/resolve` { resolution, refund_amount? } -> audited.
- `GET /v1/admin/reconciliation` -> { balanced: true, total_debits, total_credits,
  imbalance: "0.00", accounts:[...] } — the INVARIANT-2 proof surfaced.
No unrestricted endpoint may silently mutate balances; every money move is a
balanced ledger transaction with an actor + reason in the audit log.

## Concurrency & correctness requirements (prove these)
1. Every transaction balances (INVARIANT 1).
2. Whole-ledger reconciliation imbalance == 0 (INVARIANT 2).
3. No double charge: settling a job twice (idempotent) and two concurrent result
   posts both yield exactly ONE capture.
4. No negative balance under concurrency: a customer funded for exactly one job who
   submits two jobs concurrently -> exactly one hold succeeds, the other 402
   insufficient_credits; balance never < 0.
5. Failed/cancelled/timed-out job -> hold fully released, zero net charge.
6. Refund reverses the charge and the ledger still balances.

## Disclosed billing policy — minimum billable duration
Settlement meters **actual** runtime from the job's reported `duration_seconds`,
but bills for at least `BILLING_MIN_BILLABLE_SECONDS` (config, default 60s). This
is a disclosed partial-charge policy (spec §14): a sub-second job (e.g. a
simulated CPU run) still incurs a minimum charge instead of rounding to 0.00, so
provider earnings and platform revenue are non-degenerate. Set to 0 to bill pure
actual runtime. Estimated vs actual are still tracked separately on the invoice.

## Honesty
Credits are simulated/labelled. An unpaid/estimated amount is never counted as
received revenue. A failed job is never billed as success. Token market metrics are
never shown as revenue (Phase 8+). Estimated vs actual cost are tracked separately.

## Config
BILLING_SIGNUP_GRANT_MINOR, BILLING_CURRENCY="USDC_CREDITS", BILLING_CURRENCY_EXPONENT=2,
PAYMENT_PROVIDER_CONFIGURATION=simulated.
