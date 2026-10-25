# Token launch checklist: BNB Smart Chain + PancakeSwap

One document, two tracks. **Track A (testnet)** is ready to run today and needs only test BNB. **Track B
(mainnet)** is gated by things no script can do: an audit, a legal review, a real multisig and real money.

All commands run from `contracts/`. Every script below sends nothing unless told to (`--execute`, `--submit`,
or `launch.mjs`).

## 0. What is proven, and how

| Claim | Proof | Command |
|---|---|---|
| The contracts behave | 25 Solidity tests | `./node_modules/.bin/hardhat test` |
| Pool maths, config and safety rules | 9 unit tests, no network | `npm run test:js` |
| Pool tooling against the **real PancakeSwap on a fork of BSC mainnet**: refuses a wrong router, a mistyped amount, an EOA as LP holder, a pair funded by an attacker; seeds the exact price; LP lock holds 12 months then opens; strangers can buy and sell; burn mode; slippage bounds stop a skewed pair | 39 checks. Mutation-tested: disabling the pair guard or the lock cliff makes it fail | `npm run rehearsal:pool` |
| **Your exact testnet files and commands** against a fork of BSC testnet: launch, supply split, ownership, pool, buy, lock release, explorer inputs | 15 checks | `node scripts/testnet-dress-rehearsal.mjs` |
| BSC runs the Cancun opcodes the contracts are compiled for | checked live against BSC mainnet and testnet, and again by every preflight | `scripts/preflight.mjs` |
| PancakeSwap router, factory and WBNB addresses | read from the chain on every run; mainnet matches, testnet factory corrected (the old script had a wrong one) | `scripts/preflight.mjs` |

**Not yet proven** (needs things only you have): a real transaction on public BSC testnet (the deployer has 0
test BNB), and the BscScan submission itself (needs your API key; the inputs it sends are verified locally by
recompiling and matching the on-chain code).

## Track A: BSC testnet launch (test money, no value)

| # | Step | Command / action | Done when |
|---|---|---|---|
| A1 | Show the deployer address | `DOTENV_CONFIG_PATH=testnet.env node scripts/testnet-balance.mjs` | prints the address |
| A2 | Get test BNB | Faucet: https://www.bnbchain.org/en/testnet-faucet, to the deployer address. About 0.3 is plenty (the launch costs about 0.001) | balance above 0.2 |
| A3 | Fund the liquidity wallet | `DOTENV_CONFIG_PATH=testnet.env npm run testnet:fund -- <LIQUIDITY_WALLET from launch/tokenomics.testnet.json> 0.1` | liquidity wallet holds 0.1 |
| A4 | Rehearse once more on a fork | `node scripts/testnet-dress-rehearsal.mjs` | `PASSED` |
| A5 | Preflight | `EXPECT_CHAIN_ID=97 DOTENV_CONFIG_PATH=testnet.env node scripts/preflight.mjs launch/tokenomics.testnet.json --pool launch/pool.testnet.json` | `GO`. Warnings are expected on a testnet (plain-wallet "multisig", no audit file, uncommitted changes: commit first) |
| A6 | Launch the contracts | `DOTENV_CONFIG_PATH=testnet.env node scripts/launch.mjs launch/tokenomics.testnet.json` | writes `launch.97.json` with every address |
| A7 | Pool: look first | `DOTENV_CONFIG_PATH=testnet-liquidity.env node scripts/pool.mjs launch/pool.testnet.json --report launch.97.json` | prints price and "DRY RUN" |
| A8 | Pool: create | same command plus `--execute` | writes `pool.97.json` with pair, swap link |
| A9 | Verify on BscScan | `ETHERSCAN_API_KEY=<key> DOTENV_CONFIG_PATH=testnet.env node scripts/verify-launch.mjs launch.97.json --pool pool.97.json --submit` (key: https://etherscan.io/myapikey; one key covers BSC). Without `--submit` it only writes the inputs for manual upload | every line PASS |
| A10 | Wire the API (below) and restart | set the env vars, then open `/v1/settlement/contracts` and the wallet page | the site shows the real token balance |
| A11 | Publish | token address, pair, swap link (`pool.97.json`), BscScan links; in the whitepaper add the addresses | public |
| A12 | Commit the records | `launch.97.json`, `pool.97.json`, `launch/*.json` (addresses only; the `*.env` files with keys are git-ignored) | committed |

API environment after A8 (values from `launch.97.json`):

```
SETTLEMENT_ENABLED=true            SETTLEMENT_CHAIN_ID=97
CHAIN_RPC_URL=<testnet RPC>        COMPUTE_TOKEN_ADDRESS=<ComputeCoinToken>
SETTLEMENT_CONTRACT_ADDRESS=<ComputeSettlement>
PLATFORM_SIGNER_PRIVATE_KEY=<key of PLATFORM_SIGNER in the plan; testnet.env has it>
PLATFORM_TREASURY_ADDRESS=<FEE_TREASURY>
WALLET_CHAIN_NAME="BNB Smart Chain Testnet"   WALLET_EXPLORER_BASE=https://testnet.bscscan.com
```

After launch, check: the token page on testnet.bscscan.com shows 1,000,000,000 supply and the verified source; the
pair shows the reserves from `pool.97.json`; a swap on the PancakeSwap testnet page works; the LP tokens sit in the
lock address (unlock date in `pool.97.json`).

## Track B: BSC mainnet (do not start before every gate is green)

### Gates no script can clear

| Gate | Why | Owner |
|---|---|---|
| Independent smart-contract audit, every finding fixed and re-verified | The settlement contract holds user funds; the repo's own runbooks block mainnet without it | You and an audit firm |
| Freeze the audited code | `node scripts/preflight.mjs x --freeze launch/audited.json`, commit. Mainnet preflight then refuses any bytecode that differs | You |
| Legal review of the token and its distribution | Unreviewed token sales can be unlawful; the Terms and whitepaper are drafts | Counsel |
| A real Safe multisig on BSC with at least 2 signers on separate devices | One key must never hold the token or the admin powers (preflight fails on a plain wallet) | You (https://app.safe.global) |
| Real BNB for gas and liquidity | The pool size sets the opening price; thin pools are drained by bots | You |
| Final numbers: allocations, pool amounts, lock months, pause policy, timelock delay | Written into `launch/tokenomics.json` (`status: APPROVED`) and `launch/pool.json` | You |
| A private (MEV-protected) RPC | Public RPCs expose the liquidity transaction to front-running (preflight warns) | You |
| Platform signer key custody | The API holds `PLATFORM_SIGNER_PRIVATE_KEY` as a hot key. The 30% fee cap bounds the loss, but use a KMS/HSM or an ERC-1271 signer for real money | You |

### Decisions that change what buyers see

| Decision | Choices | Consequence |
|---|---|---|
| Token owner (`tokenOwner`) | `safe`, `timelock`, `renounce` | `safe`: can pause all trading instantly (scanners flag it). `timelock`: pause is delayed, so it is not an emergency stop. `renounce`: no pause ever, and no emergency stop either |
| Settlement owner (`settlementOwner`) | `safe`, `timelock` | `timelock` makes rotating a leaked signer slow (the delay). `safe` is faster but trusts the signers |
| LP handling (`lp.mode`) | `lock` (ComputeVesting, N months, then to the Safe), `burn` (dead address), `custom` (a locker contract you have read) | A plain wallet is refused on mainnet |
| Pool amounts | `tokenAmount`, `nativeAmount` | These two numbers ARE the launch price (`nativeAmount / tokenAmount`) |

### Mainnet runbook

| # | Step | Command | Done when |
|---|---|---|---|
| B1 | Create `contracts/mainnet.env` (git-ignored by `*.env`) | `RPC_URL=<private RPC>`, `PRIVATE_KEY=<fresh deployer key, funded only for gas>` | file exists |
| B2 | Write final plans | `launch/tokenomics.json` (copy the example; `status: APPROVED`; real Safe address) and `launch/pool.json` (copy `pool.example.json`) | reviewed by a second person |
| B3 | Rehearse the tooling on a mainnet fork | `npm run rehearsal:pool` | 39/39 PASS |
| B4 | Preflight | `EXPECT_CHAIN_ID=56 I_UNDERSTAND_MAINNET=yes DOTENV_CONFIG_PATH=mainnet.env node scripts/preflight.mjs launch/tokenomics.json --pool launch/pool.json` | `GO`, and every WARN has an owner who can explain it |
| B5 | Launch the contracts | `I_UNDERSTAND_MAINNET=yes DOTENV_CONFIG_PATH=mainnet.env node scripts/launch.mjs launch/tokenomics.json` | `launch.56.json` written |
| B6 | Verify on BscScan | as A9 with `launch.56.json` | all PASS |
| B7 | Re-run preflight with the live token | add `--report launch.56.json` | "pair is untouched" PASS |
| B8 | Pool dry run, then create | Use a key file for the **liquidity wallet** (`PRIVATE_KEY`, `RPC_URL`). Dry run first. Then `I_UNDERSTAND_MAINNET=yes CONFIRM_TOKEN_AMOUNT=<tokenAmount string> CONFIRM_NATIVE_AMOUNT=<nativeAmount string> node scripts/pool.mjs launch/pool.json --report launch.56.json --execute` | `pool.56.json` written; pair verified |
| B9 | Announce only now | Publish the token, pair, lock and BscScan links; update the whitepaper addresses | |
| B10 | Watch | Reserves, LP lock, token owner, and the Safe's pending transactions | |

Do not send COMPUTE to anyone you do not control before B8: the first liquidity fixes the price, and the pool
tooling aborts if someone has already seeded the pair.

## If something goes wrong

| Situation | What happens / do this |
|---|---|
| `pool.mjs` says the pair "already holds funds" | Someone seeded it first. Do not continue. Find out who; do not add liquidity at the planned price |
| The pool transaction reverts | The slippage bound did its job (the pair price moved). Nothing was spent beyond gas. Re-run the dry run, then retry |
| The wrong amount was sent | Mainnet needs the amounts typed twice precisely to prevent this; if it happened the LP is already locked or burned, so check the numbers before B8 |
| Launch stops half-way | Only gas is lost: re-run preflight, deploy fresh (new addresses), and abandon the partial deployment. Do not announce anything from it |
| BscScan verification fails | Upload `verification/<chainId>/<Contract>.<address>/standard-input.json` and `constructor-args.hex` by hand on BscScan (Solidity Standard-Json-Input) |
| A signer key leaks | The fee cap (30%) bounds the loss per job. Rotate the signer through the settlement owner (timelock: wait the delay). Pause settlement if the owner can |
| A bug is found in the token | Only the owner can pause. Decide this before launch (`tokenOwner`), not during an incident |

## Files this tooling uses

| File | Purpose | In git |
|---|---|---|
| `launch/tokenomics*.json` | Allocation plan, wallets, timelock delay, owners | yes (addresses only) |
| `launch/pool*.json` | Pool amounts, slippage, LP handling | yes |
| `launch/audited.json` | Frozen audited bytecode hashes | yes (after the audit) |
| `launch.<chainId>.json`, `pool.<chainId>.json` | Records of what was deployed | yes |
| `testnet.env`, `testnet-wallets.env`, `testnet-liquidity.env`, `mainnet.env` | Keys | **never** (git-ignored `*.env`) |
| `scripts/lib/pool.mjs`, `pool.mjs`, `preflight.mjs`, `verify-launch.mjs` | The tooling | yes |
