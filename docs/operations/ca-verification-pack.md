# COMPUTE token: CA verification pack (for review and signature)

**Status: DRAFT, NOT SIGNED.** This pack was prepared from the project's own files so a Chartered Accountant (CA)
can check the figures and, if satisfied, sign Part 6. It is not a certificate, an audit or a legal opinion, and it
becomes none of those until a qualified professional has verified it and signed it. Nobody should present it as
signed before then.

Prepared: 2026-11-14. Source files: `contracts/launch/tokenomics.example.json`, `contracts/launch/pool.example.json`,
`contracts/src/ComputeCoinToken.sol`, `WHITEPAPER.md`, `docs/operations/launch-checklist.md`.

> The allocation and pool figures below are **PLACEHOLDERS** in the repo (`status: PLACEHOLDER`). The owner must
> confirm them (Part 2, Part 3) before the CA signs. The mainnet launch script refuses to run unless the plan is
> set to `APPROVED`.

## Part 1. What is being certified

| Item | Value | Source |
|---|---|---|
| Token name / symbol | ComputeCoin / COMPUTE | `ComputeCoinToken.sol` |
| Network | BNB Smart Chain (chain id 56). Testnet rehearsal is chain 97 | `launch-checklist.md` |
| Decimals | 18 | ERC-20 standard |
| Total supply | **1,000,000,000 COMPUTE**, minted once in the constructor. The contract has no mint function, so supply cannot increase | `ComputeCoinToken.sol` (`MAX_SUPPLY`), `WHITEPAPER.md` |
| DEX | PancakeSwap V2 | `pool.example.json` |
| Token contract address | ____________________ (fill in after launch; from `launch.56.json`) | |

## Part 2. Allocation of the supply (arithmetic check)

Shares are written in basis points (1 bp = 0.01%). The CA should confirm the shares add up to exactly 10,000 bps
and that each token amount equals share × total supply.

| # | Allocation | Share | bps | Tokens | Holding method | Beneficiary | Cliff | Vesting |
|---|---|---|---|---|---|---|---|---|
| 1 | Liquidity | 10% | 1,000 | 100,000,000 | Direct | Liquidity wallet | none | none |
| 2 | Treasury | 25% | 2,500 | 250,000,000 | Direct | Multisig (Safe) | none | none |
| 3 | Ecosystem | 25% | 2,500 | 250,000,000 | Vesting contract | Multisig (Safe) | 0 months | 48 months linear |
| 4 | Team | 15% | 1,500 | 150,000,000 | Vesting contract | Team wallet | 12 months | 48 months linear |
| 5 | Community | 15% | 1,500 | 150,000,000 | Direct | Multisig (Safe) | none | none |
| 6 | Reserve | 10% | 1,000 | 100,000,000 | Vesting contract | Multisig (Safe) | 6 months | 24 months linear |
| | **Total** | **100%** | **10,000** | **1,000,000,000** | | | | |

Checks: 1,000 + 2,500 + 2,500 + 1,500 + 1,500 + 1,000 = 10,000 bps. 100 + 250 + 250 + 150 + 150 + 100 = 1,000 million tokens.

Points for the CA to raise with the owner:

- The whitepaper (section on allocation) shows the same split. An older whitepaper used a different split
  (40/20/15/15/10). Confirm the owner means this one, and that no earlier document is still in circulation.
- Immediately available at launch (not vested): liquidity, treasury and community = 50% (500,000,000 COMPUTE).
  Vested (locked at launch): ecosystem, team and reserve = 50%.
- Treasury, ecosystem, community and reserve all go to one Safe. State who the signers are (Part 4).

## Part 3. Opening liquidity pool

| Item | Value | Note |
|---|---|---|
| COMPUTE put into the pool | 100,000,000 | Equals the Liquidity allocation (10%) |
| BNB put into the pool | 10 BNB | **Real money. Source of funds to be documented below** |
| Implied opening price | 0.0000001 BNB per COMPUTE (10 / 100,000,000) | Set by these two numbers alone |
| Implied opening value of pool | 10 BNB on each side, so 20 BNB in total | In INR/USD at the rate on the launch date: ____________ |
| Implied fully diluted value at opening price | 1,000,000,000 × 0.0000001 = 100 BNB | |
| LP tokens | Locked 12 months (ComputeVesting), then released to the Safe | `pool.example.json` |
| Slippage limit / deadline | 1% / 600 seconds | |

Source of the 10 BNB (owner to complete; CA to verify against bank / exchange statements):

| Question | Answer |
|---|---|
| Whose funds are they, and where did they come from (salary, business income, exchange purchase, other)? | |
| Purchase date, amount paid in fiat, platform and transaction ID | |
| Wallet that holds the BNB before the launch (address) | |
| Is it the owner's personal money or the company's? | |

## Part 4. Wallets (owner to fill in, CA to cross-check with identity and ownership evidence)

| Role | Address | Who controls it | Evidence seen by CA |
|---|---|---|---|
| Deployer (pays gas, deploys contracts) | | | |
| Safe multisig (owns the token, holds treasury, ecosystem, community, reserve) | | Signers: | |
| Liquidity wallet | | | |
| Team wallet | | | |
| Platform signer | | | |
| Fee treasury | | | |

The testnet addresses in `launch/tokenomics.testnet.json` are throwaway wallets and must **not** be used on mainnet.

## Part 5. Items the CA can verify, and items outside a CA's scope

Within a CA's scope (tick as verified):

| # | Check | Verified | Notes |
|---|---|---|---|
| 1 | Allocation shares add to 100% and token amounts match Part 2 | [ ] | |
| 2 | Pool figures in Part 3 agree with the Liquidity allocation and the final plan file | [ ] | |
| 3 | The plan file shows `status: APPROVED` and matches this pack, and the whitepaper agrees | [ ] | |
| 4 | Source of the BNB for liquidity is documented and consistent with the owner's records | [ ] | |
| 5 | Wallet addresses in Part 4 are owned by the people or entity named | [ ] | |
| 6 | Team wallet beneficiary and vesting terms are recorded in writing (agreement with the beneficiary) | [ ] | |
| 7 | Accounting and tax treatment is decided (see below) | [ ] | |

Accounting and tax questions for the CA to answer (this pack does not answer them): how the BNB used for liquidity
is booked and valued; how tokens held by the owner or company are recognised; whether a transfer of BNB for
LP tokens is a taxable event; how the Indian provisions on virtual digital assets apply (including TDS and the
reporting requirements), and how any later sale of COMPUTE is taxed. If the entity is a company, whether the board
has approved the issue and the liquidity funding.

**Outside a CA's scope and not covered by any signature on this pack:**

- **Smart-contract security.** An independent audit firm must review the contracts. The repo's own runbooks block
  mainnet without it. A CA cannot attest that the code is safe.
- **Legal status of the token and its distribution.** Counsel must review whether the token or its sale is
  regulated, and the Terms and whitepaper are still drafts.

## Part 6. Verification and signature (CA to complete)

I have examined the documents listed below and the figures in Parts 1 to 5. My conclusion is limited to the
checks ticked in Part 5. I give no opinion on smart-contract security or on the legal status of the token.

Documents examined: ______________________________________________________________

Exceptions or qualifications: ____________________________________________________

| | |
|---|---|
| Name of CA | |
| Membership number (ICAI) | |
| Firm name and registration number | |
| UDIN (if applicable) | |
| Date | |
| Place | |
| Signature and seal | |

## Part 7. Does a CA signature unlock PancakeSwap?

No. Creating a token pool on PancakeSwap is permissionless. It needs no approval, listing fee or signature from
PancakeSwap, a CA or anyone else: once the contracts are deployed and liquidity is added, the token can be traded
on PancakeSwap. This pack is for **your own protection and for third parties who ask for diligence** (exchanges,
auditors, banks, investors, listing sites such as CoinMarketCap or CoinGecko). It is not a technical requirement.

What actually has to happen before a real launch is in `docs/operations/launch-checklist.md`, Track B. In short: an
independent audit, legal review, a real multisig, final numbers marked `APPROVED`, and real BNB. A testnet launch
(Track A) can run any time and needs only test BNB.
