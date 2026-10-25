# ADR-0004: How COMPUTE tokens and the platform ledger relate

Status: **proposed, needs owner decisions (section 5). Phase 1 is built; phase 2 is not.**

## 1. Context

Two independent balances exist:

- **Credits**: rows in the double-entry ledger (Postgres). Simulated today. Customers spend them on jobs, hosts earn them.
- **COMPUTE tokens**: an ERC-20 on a blockchain. Today only on test networks, with no value.

Nothing connects them. If both ever represent real value, a user could hold 100 credits and 100 tokens that are
"the same money", and any code path that moves one without the other makes them disagree.

## 2. What is built (phase 1, read-only and safe)

- A user proves control of a wallet by signing a server-issued message (`POST /v1/wallet/link`). One address can belong
  to one account per chain, so two accounts can never claim the same tokens.
- `GET /v1/wallet` shows the credits and the linked wallet's on-chain COMPUTE balance side by side.
- Per-job escrow (`ComputeSettlement`) and `chain_reconciler` already compare on-chain escrow state to the ledger.
- Nothing moves value between credits and tokens. The page says so.

## 3. The invariant any bridge must keep

For tokens held by the platform (the deposit address) and credits issued against them:

    tokens received at the deposit address  -  tokens withdrawn from it  =  credits issued against deposits  -  credits redeemed for withdrawals

Checked by a reconciliation job that fails loudly on any difference. Everything below exists to make that equality hold.

## 4. Options

| Option | How | Disagreement risk | Cost |
|---|---|---|---|
| A. Separate rails (current) | Tokens never become credits. Per-job escrow only. | None by construction, but two sources of truth for money | Done |
| B. Deposit and withdraw bridge | Deposit: user sends COMPUTE to the platform address, the backend sees the `Transfer` event after N confirmations and credits the ledger once (idempotent on tx hash + log index). Withdraw: the ledger debits first, then the platform pays out on-chain; a failed payout is reversed. | Controlled by the invariant above | Largest. Needs a chain watcher, a payout signer and limits |
| C. Credits only, tokens as a reward | Tokens are minted or sent to hosts as incentives; never accepted as payment | Low | Medium |

**Recommendation: B, built in stages and only after the contracts are audited.** Stage 1 deposits only (cannot lose
platform funds, only mis-credit); stage 2 withdrawals with daily limits and a pause switch.

## 5. Decisions needed from the owner before building B

1. **Rate:** is 1 COMPUTE worth a fixed number of credits, or does it follow a market price? A market price needs an oracle and
   rules for volatility, and exposes hosts and customers to price swings. A fixed rate is simpler but can be arbitraged once a
   market exists.
2. **Direction:** deposits only, or withdrawals too?
3. **Limits:** per-transaction and daily caps, minimum deposit, and confirmation depth (blocks to wait, to survive reorgs).
4. **Gas:** who pays on withdrawal, and in what coin?
5. **Legal:** whether accepting and paying out a token counts as a regulated activity where you operate (a lawyer's question,
   not an engineering one). Do not enable B on mainnet before this is answered.

## 6. Testing plan for B (all of it runs on a local chain before any public network)

- Deposit seen exactly once even if the watcher restarts or the same block is rescanned.
- A reorged deposit is not credited (or is reversed).
- Withdrawal debits first; a failed on-chain payout restores the balance; a double-submit pays once.
- The reconciliation job passes after each scenario and fails when a row is deliberately corrupted.
- Fuzz the amount inputs with the strict `parse_money` already used for credits.

## 7. Not done, deliberately

No deposit watcher, no withdrawal signer, no token-to-credit conversion. Phase 1 only makes balances visible and binds
addresses to accounts, which is safe to ship and is required by every option above.
