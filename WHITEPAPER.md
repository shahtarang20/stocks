# ComputeCoin ($COMPUTE): A Marketplace for AI GPU Compute, Settled On-Chain

**Technical Whitepaper v1.1: Developer Preview**
*October 2026*

> **Read this first.** ComputeCoin is a developer preview. This document separates what is built and tested
> from what is not built. Nothing in it is an offer, a promise of future value, or investment advice. The smart
> contracts have **not** been audited by an independent firm.

---

## 1. Summary

AI teams rent GPUs from a few large clouds at high margins, while independent data centres and workstation
owners leave capacity idle. ComputeCoin is a marketplace that connects the two: providers run an agent on their
NVIDIA GPUs, customers submit containerised jobs through a console, API or SDK, and payment is escrowed and
settled against metered runtime. Job scheduling and metering run off-chain for speed. An optional on-chain
escrow settles payments in the **$COMPUTE** token on BNB Smart Chain.

## 2. Status today

| Component | Status |
|---|---|
| Web console, REST API, Python and TypeScript SDKs | Built and in developer preview |
| Provider agent (NVIDIA GPUs only; AMD, Intel and Apple GPUs are not supported) | Built. Hardware it reports is self-reported and labelled "Detected", not independently verified |
| Job execution | Containerised runner is implemented. **It has not yet been validated on real GPU hardware; jobs run in simulated mode by default** |
| Customer credits and payments | **Credits are test credits.** A Stripe test-mode payment rail is built; live payments are not enabled |
| `ComputeCoinToken` (ERC-20) | Built and unit-tested. **Not audited** |
| `ComputeSettlement` (escrow) | Built and unit-tested (25 contract tests). **Not audited** |
| Timelock and vesting contracts | Built from unmodified OpenZeppelin components. Used to hold and release allocations |
| Public deployment | Planned first on BNB Smart Chain **testnet** (test coins, no monetary value). Not deployed on mainnet |
| External security audit | **Not done** |
| Legal review of the token | **Not done** |

## 3. Architecture

- **Off-chain orchestration.** An API schedules jobs, reserves machine capacity atomically (database row locks and
  expiring leases), meters runtime per second, and keeps a double-entry ledger in which debits equal credits.
- **Providers.** `computecoin-agent` detects NVIDIA GPUs through NVML, sends signed heartbeats every 30 seconds, and
  runs workloads in a locked-down container. A machine that misses heartbeats for 90 seconds is marked offline and
  its reservations are re-queued.
- **Quotes and reservations.** A quote fixes the price for a short window (120 seconds by default). Accepting it moves funds from available
  balance into a reservation; completion captures the metered cost, failure releases the hold.
- **On-chain settlement (optional).** `ComputeSettlement` escrows tokens for a job and pays out against a voucher
  signed by the platform (section 6).

## 4. The $COMPUTE token

| Property | Value |
|---|---|
| Name and symbol | ComputeCoin, `COMPUTE` |
| Standard | ERC-20 with EIP-2612 permit, 18 decimals |
| Supply | 1,000,000,000, minted once, in the constructor. **There is no mint function**, so supply can never increase |
| Burning | Any holder may burn their own tokens. Nothing burns tokens automatically |
| Pause switch | The token owner can pause **all transfers**, which also halts trading. Who holds that power (a multisig, a timelock, or nobody if ownership is renounced) is published with the launch |
| Chain | BNB Smart Chain (testnet first) |

### 4.1 Distribution (proposed; final figures and addresses are published at launch)

| Allocation | Share | Terms |
|---|---|---|
| Liquidity | 10% | Seeds the PancakeSwap pool; the resulting LP tokens are locked or burned (section 7) |
| Treasury | 25% | Multisig |
| Ecosystem | 25% | Vesting contract, linear over 48 months |
| Team | 15% | Vesting contract, 12-month cliff then linear over 48 months |
| Community | 15% | Multisig |
| Reserve | 10% | Vesting contract, 6-month cliff then linear over 24 months |

The launch tooling splits the supply exactly to these shares, locks the vested shares in vesting contracts, and
leaves the deploying key with zero tokens and no rights. This is checked by an automated rehearsal before launch.

### 4.2 What the token does today

1. It is the settlement asset of the escrow contract in section 6.
2. Deposits: COMPUTE sent from a wallet you have linked to your account can be credited to your platform balance.
   This is built, tested and **switched off by default**; the exchange rate has not been set.

### 4.3 What is not built

The following are ideas, **not** features. They are not implemented and nothing here commits the project to build
them: provider staking and slashing, fee discounts for paying in COMPUTE, automatic buy-back and burn, on-chain
governance voting, mining or emission rewards, and bridges to other chains.

## 5. Control of the contracts

- The token owner and the settlement owner are set at launch to a multisig and a timelock. A change made through
  the timelock is visible on-chain for the full delay before it can run; the delay and the owners are published
  with the launch.
- Anyone can read these settings on BscScan once the contracts are verified.

## 6. Settlement contract

`ComputeSettlement` is a non-custodial escrow:

1. **`lockBudget(jobId, provider, maxCost, deadline)`**: the customer locks the job's maximum budget. The escrow is
   keyed by the customer's address and the job id, so nobody can occupy another customer's job id.
2. **`settleJob(...)`**: pays the provider, the fee treasury and refunds the unused budget, against a voucher signed
   by the platform signer. A voucher is bound to the customer, job, amounts, escrow nonce, expiry, chain and
   contract, so it cannot be replayed, reused or moved to another contract or chain.
3. **`refundJob(...)`**: after the deadline the customer can always withdraw the locked amount with no voucher.
   Before the deadline a platform cancellation voucher is needed. Refunds still work while the contract is paused.

What the contract guarantees: tokens only leave through settlement or refund, to the provider or customer recorded
at lock time; the protocol fee is capped at **30%** of the final cost by an immutable constant, so a compromised
signing key cannot sweep an escrow; every settlement conserves tokens exactly.

What it does not remove: the **final cost is set by a voucher signed by the platform**. The customer is protected by
the budget they locked and by the deadline refund, but they trust the platform's metering within that budget. The
owner can rotate the signer and the fee treasury and can pause new locks and settlements.

## 7. Trading

When the token is launched publicly, liquidity is seeded on **PancakeSwap V2** on BNB Smart Chain against BNB.
The first deposit sets the opening price, so the pool and its price are published before anyone trades. The
pool-creation tooling refuses to proceed if the pair has already been funded by someone else, bounds the
slippage of the deposit, and sends the LP tokens to a burn address or a time lock, so the project cannot withdraw
the pool. The market price is set by trading and is not set or guaranteed by the project. Contract and pool
addresses are published here and on BscScan after deployment, and only those addresses are genuine.

## 8. Risks

- **Smart-contract risk.** The contracts are unaudited. Bugs can lose funds permanently.
- **Control risk.** The owner can pause token transfers and the settlement contract, and can change the platform
  signer and fee treasury (behind a timelock if configured).
- **Operator trust.** Metering and voucher signing are run by the platform, which holds the signing key.
- **Execution risk.** Real-GPU execution is unvalidated; jobs are simulated in this preview.
- **Market risk.** A token can lose all of its value. Liquidity may be thin and prices volatile.
- **Regulatory risk.** The legal treatment of tokens differs by country and is not yet reviewed.
- **Preview software.** Features may change or break. Demo data is synthetic.

## 9. Roadmap (no dates are promised)

1. Deploy to BNB Smart Chain testnet, verify the contracts on BscScan, publish the addresses and run a public
   testnet pool.
2. Validate real GPU execution on hardware and enable live payments.
3. Independent smart-contract audit; fix and re-verify every finding; freeze the audited bytecode.
4. Legal review of the token and its distribution.
5. Only after steps 3 and 4: consider a mainnet deployment.
