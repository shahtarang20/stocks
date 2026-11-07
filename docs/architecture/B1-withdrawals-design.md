# B1: Token withdrawals (design note)

Status: **code written, OFF by default. It needs the owner's approval of this note and of the ADR-0004 section 5 answers
before anyone sets `WITHDRAWALS_ENABLED=true`. It is money code (rule L3): an independent review is required before merge,
and mainnet stays off until the contract audit and the legal review (ADR-0004 section 5, item 5).** The code has been
written but **not run**: no test has been executed yet.

## 1. What it does

A user turns credits into COMPUTE tokens, paid on-chain to their own wallet. Credits leave the ledger first; tokens are sent
second; a failure puts the credits back with a record.

## 2. Request

`POST /v1/withdrawals` with `{amount, destination}` and a required `Idempotency-Key` header.
* `destination` must equal the caller's **signature-verified `LinkedWallet`** on the configured chain. Any other address, an
  unlinked account, or another chain is refused. Nothing else is ever paid.
* Amount is credits (strict decimal, 2 places). Tokens = credits / `TOKEN_CREDITS_PER_TOKEN`, rounded **down**.
* Checks: minimum, per-transaction cap, per-user and global rolling 24h caps, email verified, rate limit, not paused.
* A repeat with the same key returns the same withdrawal; the same key with other values is 409.

## 3. Ledger (existing `ledger.post_transaction`, one balanced transaction per change)

| Step | Debit | Credit | Kind |
|---|---|---|---|
| request | customer_credits | withdrawal_pending | payout_request |
| paid out | withdrawal_pending | paid_out | payout_settle |
| failed / cancelled | withdrawal_pending | customer_credits | refund (with `refund_reason`) |

Two new platform account types (`withdrawal_pending`, `paid_out`); `withdrawal_pending` can never go negative. Every change
also writes an audit record.

## 4. Payout worker (`services/withdrawals.py::run_payout_cycle`, a step in `app/worker.py` with its own try/except)

* **Signer is injectable** (`WithdrawalSigner`). The production one, `KmsWithdrawalSigner`, signs through a `KmsAdapter`.
  `AwsKmsAdapter` (AWS KMS, secp256k1 key) is the adapter the worker uses (`build_default_signer`); it has not been run against a
  real key. `StubKmsAdapter`, the default when no adapter is passed, raises `NotConfigured`. The key is never in a file, a setting
  or the database; only a key id and the public hot-wallet address are settings. Approving the KMS/HSM product is still an owner decision.
* **One in-flight transaction per hot wallet:** an advisory lock, a single-pass worker, and a unique partial index on
  `hot_wallet` where status is `sending` or `broadcast`.
* The signed transaction and its hash are stored **before** broadcast, so a crash re-sends the same transaction.
* Waits `WITHDRAWALS_CONFIRMATIONS` blocks. A receipt that disappears (reorg) is treated as not yet mined.
* Receipt status 1 and confirmed: `paid_out`. Receipt status 0 (revert): refunded, reason `reverted`.
* **Timeout:** refund only when the nonce is provably used by a different transaction (`timeout_nonce_used`). If the nonce is
  still free the transaction could still mine, and refunding it would pay twice, so the system **pauses** and an admin decides.
  (The brief said "timeout -> refund"; this is the safe reading and is the one open design question for the owner.)
* Without a configured signer, requests stay `pending`; an admin can cancel (refund) any withdrawal that was never signed.
* Waits (no refund) when the hot wallet lacks tokens or the gas price is above `WITHDRAWALS_MAX_GAS_PRICE_WEI`.

## 5. Reconciliation (`chain_reconciler.reconcile_withdrawals`, run by the worker and `GET /v1/admin/withdrawals/reconciliation`)

Reads `Transfer` events sent by the hot wallet (only blocks that are confirmed) and compares them with withdrawal rows and the
ledger. Any of these **pauses withdrawals automatically**: an unknown transfer, a different amount or destination, a paid-out
withdrawal with no chain transfer, `paid_out` account differing from paid-out withdrawals, `withdrawal_pending` differing from
open withdrawals. An admin resumes after investigating.

## 6. Kill switches

`WITHDRAWALS_ENABLED` (default false; also needs RPC URL, KMS key id, hot-wallet address, a valid rate and ordered limits, see
`Settings.withdrawals_configured`) plus the admin/automatic pause (`POST /v1/admin/withdrawals/pause`). While paused, no request
is accepted and nothing is sent; in-flight payouts are still watched and finished.

## 7. Settings that need an owner decision (all placeholders)

`TOKEN_CREDITS_PER_TOKEN` (rate; shared with deposits), `WITHDRAWALS_MIN_CREDITS` 10.00, `WITHDRAWALS_MAX_PER_TX_CREDITS` 100.00,
`WITHDRAWALS_DAILY_USER_CREDITS` 250.00, `WITHDRAWALS_DAILY_GLOBAL_CREDITS` 2000.00, `WITHDRAWALS_CONFIRMATIONS` 12,
`WITHDRAWALS_TIMEOUT_SECONDS` 3600, `WITHDRAWALS_MAX_GAS_PRICE_WEI` (gas: who pays; here the platform pays gas from the hot
wallet's native coin), the hot-wallet float, KMS/HSM product, legal answer.

## 8. Files

`models/withdrawal.py`, migration `0025_withdrawals`, `services/withdrawals.py`, `withdrawal_signer.py`, `withdrawal_chain.py`,
`withdrawal_pause.py`, `chain_reconciler.py` (additions), `schemas/withdrawal.py`, `api/routers/withdrawals.py`, `worker.py`,
small additions in `ledger.py` (two account types, `withdrawal_pending` non-negative) and `config.py`.
Tests (written, not run): `tests/test_b1_withdrawals.py`. Still to do: local Hardhat end-to-end (reorg below confirmations,
real revert), the KMS adapter, and the independent review.
