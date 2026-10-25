# ComputeCoin (COMPUTE) — Testnet Token Launch Runbook

> **Goal:** Deploy the real `ComputeCoinToken.sol` ERC-20 contract to a public testnet for **₹0 / $0**, receive the 1 Billion `COMPUTE` genesis supply into your MetaMask wallet, view your live token on a public block explorer (Etherscan / Basescan / BscScan), and create a testnet decentralized exchange (DEX) liquidity pool so people can trade it with test money.

> **Superseded for real launches by [launch-checklist.md](launch-checklist.md).** That document covers the
> safe path (multisig, timelock, vesting, locked liquidity, preflight, verification, rehearsals). This runbook
> keeps the beginner walkthrough for a throwaway single-key token. The old `create:pool` command has been
> replaced by `scripts/pool.mjs`, which checks the DEX addresses against the chain, refuses a pair someone
> funded first, bounds slippage and locks the LP tokens.

---

## Prerequisites (100% Free)

1. **MetaMask Wallet**: Installed in your browser ([metamask.io](https://metamask.io)).
2. **Node.js 18+ & npm**: Already installed in your environment.
3. **5 minutes of your time**.

---

## Supported Networks

You can deploy to any of these three popular testnets:

| Network | Gas Currency | Block Explorer | DEX Router |
| :--- | :--- | :--- | :--- |
| **Ethereum Sepolia** *(Recommended)* | SepoliaETH | [sepolia.etherscan.io](https://sepolia.etherscan.io) | Uniswap V2 |
| **Base Sepolia** | Base Sepolia ETH | [sepolia.basescan.org](https://sepolia.basescan.org) | Uniswap V2 (Base) |
| **BNB Chain Testnet** | tBNB | [testnet.bscscan.com](https://testnet.bscscan.com) | PancakeSwap V2 |

---

## Step 1: Set Up Your Testnet Wallet in MetaMask

1. Open your MetaMask browser extension.
2. In the top-left network dropdown, enable **"Show test networks"**.
3. Select **Sepolia** (or **Base Sepolia** / **BNB Smart Chain Testnet**).
4. Click your account name to **copy your wallet address** (starts with `0x...`).

---

## Step 2: Grab Free Testnet Gas Coins (Faucets)

You only need a tiny fraction of test coins (0.01 to 0.05 ETH/BNB) to deploy the contract and fund the liquidity pool. Faucets give these away for free:

### For Ethereum Sepolia:
- **Google Cloud Web3 Faucet** *(No login, fastest)*:  
  👉 [https://cloud.google.com/application/web3/faucet/ethereum/sepolia](https://cloud.google.com/application/web3/faucet/ethereum/sepolia)
- **Alchemy Sepolia Faucet**:  
  👉 [https://sepoliafaucet.com](https://sepoliafaucet.com)
- **Chainlink Faucet**:  
  👉 [https://faucets.chain.link/sepolia](https://faucets.chain.link/sepolia)

### For Base Sepolia:
- **Coinbase Base Faucet**:  
  👉 [https://www.coinbase.com/faucets/base-ethereum-sepolia-faucet](https://www.coinbase.com/faucets/base-ethereum-sepolia-faucet)

### For BSC Testnet:
- **BNB Chain Official Faucet**:  
  👉 [https://www.bnbchain.org/en/testnet-faucet](https://www.bnbchain.org/en/testnet-faucet)

*Paste your wallet address from Step 1 and click "Receive" or "Request". The test coins arrive in your MetaMask within seconds.*

---

## Step 3: Configure `contracts/.env`

Export your testnet private key from MetaMask:
1. In MetaMask, click the **three dots (⋮) → Account details → Show private key**.
2. Enter your MetaMask password and copy the 64-character private key.

Now, in your terminal:
```bash
cd contracts
cp .env.example .env
```

Open `contracts/.env` and paste your private key:
```env
DEPLOYER_PRIVATE_KEY=0xYOUR_COPIED_PRIVATE_KEY_HERE
```

> [!CAUTION]
> **Security Rule**: Never share your private key, and never put real mainnet funds in this wallet. The `contracts/.env` file is git-ignored and will never be committed to GitHub.

---

## Step 4: Deploy Your Coin (One Command)

From the `contracts/` directory, run:

```bash
# To deploy on Ethereum Sepolia (Recommended):
npm run deploy:token -- --network sepolia

# Or on Base Sepolia:
npm run deploy:token -- --network base-sepolia

# Or on BSC Testnet:
npm run deploy:token -- --network bsc-testnet
```

### What the script does automatically:
1. Connects to the testnet blockchain.
2. Verifies your deployer gas balance.
3. Deploys `ComputeCoinToken.sol` with 1 Billion `COMPUTE` max supply.
4. Mints all **1,000,000,000 COMPUTE** directly to your MetaMask address.
5. Saves a receipt in `contracts/deployments/{network}-token.json`.
6. Prints a **live Block Explorer URL** for your token!

---

## Step 5: See Your Coin on the Block Explorer & in MetaMask

### 1. View it on the public Explorer
Open the link printed in your terminal (for example: `https://sepolia.etherscan.io/token/0x...`).  
You will see:
- Token Name: **ComputeCoin**
- Symbol: **COMPUTE**
- Total Supply: **1,000,000,000 COMPUTE**
- Holders: 1 (your address holding 100%)

Anyone in the world can open this link and see your coin.

### 2. Add it to MetaMask
1. In MetaMask, scroll down on the Assets tab and click **"Import tokens"**.
2. Click the **"Custom token"** tab.
3. Paste the **Contract Address** printed in Step 4.
4. Token Symbol (`COMPUTE`) and Decimals (`18`) will auto-populate.
5. Click **"Add Custom Token"** $\rightarrow$ **"Import"**.
6. Your MetaMask now displays **1,000,000,000 COMPUTE**!

---

## Step 6: Create a Liquidity Pool (Make it Tradable)

The pool is created by `scripts/pool.mjs` from a small config file (see `launch/pool.testnet.json`):

```bash
# From contracts/ ; look first (sends nothing), then create:
DOTENV_CONFIG_PATH=testnet.env node scripts/pool.mjs launch/pool.testnet.json --report launch.97.json
DOTENV_CONFIG_PATH=testnet.env node scripts/pool.mjs launch/pool.testnet.json --report launch.97.json --execute
```

The key must belong to the wallet that holds the COMPUTE for the pool (the plan's `LIQUIDITY_WALLET`). It checks the
router, factory and WBNB against the chain, refuses a pair that already holds funds, bounds the slippage on both
sides, approves exactly the pool amount, sends the LP tokens to a lock (or the burn address), then verifies the
reserves and prints the PancakeSwap swap link. Only BNB Smart Chain (PancakeSwap V2) is configured.

## Step 7: Verify Contract Source Code (Optional)

To show the green **"Verified"** checkmark on Etherscan:

1. Get a free API key from [etherscan.io/myapikey](https://etherscan.io/myapikey).
2. Add it to `contracts/.env`:
   ```env
   ETHERSCAN_API_KEY=YOUR_ETHERSCAN_API_KEY
   ```
3. Run:
   ```bash
   npx hardhat verify --network sepolia <TOKEN_ADDRESS> <YOUR_WALLET_ADDRESS>
   ```

---

## Summary of Commands

```bash
cd contracts
# deploy (split supply, timelock, vesting) and create the pool: see launch-checklist.md
DOTENV_CONFIG_PATH=testnet.env node scripts/launch.mjs launch/tokenomics.testnet.json
DOTENV_CONFIG_PATH=testnet-liquidity.env node scripts/pool.mjs launch/pool.testnet.json --report launch.97.json --execute
```

---

## Quick path used by the project's QA (BNB testnet, two contracts in one command)

Keep testnet settings in a **separate** file, `contracts/testnet.env` (git-ignored). Do not put a
testnet key in `contracts/.env`: the local-chain scripts and tests read `.env` too, and a funded-nothing
testnet key there makes local deployments fail.

```bash
cd contracts
# testnet.env holds RPC_URL, PRIVATE_KEY (testnet only), PLATFORM_SIGNER_ADDRESS, EXPLORER_BASE
DOTENV_CONFIG_PATH=testnet.env node scripts/testnet-balance.mjs      # shows the address to fund and whether it is READY
# fund that address from https://www.bnbchain.org/en/testnet-faucet, then:
npx hardhat compile
DOTENV_CONFIG_PATH=testnet.env node scripts/deploy-testnet.mjs        # deploys ComputeCoinToken + ComputeSettlement
```

The script writes `contracts/deployments.97.json` with both addresses. To make the website show real balances,
set these on the API (and keep `WALLET_*` in sync with the chain):

```
CHAIN_RPC_URL=<same RPC>            COMPUTE_TOKEN_ADDRESS=<token>
SETTLEMENT_CONTRACT_ADDRESS=<settlement>   SETTLEMENT_CHAIN_ID=97
WALLET_CHAIN_NAME="BNB Smart Chain Testnet"  WALLET_EXPLORER_BASE=https://testnet.bscscan.com
PLATFORM_SIGNER_PRIVATE_KEY=<the signer key matching PLATFORM_SIGNER_ADDRESS>
```

Testnet keys are throwaway. Never reuse them on mainnet.

---

## Launching with a multisig, a timelock and vesting (instead of one key holding everything)

`scripts/deploy-testnet.mjs` mints the whole supply to one address that also keeps the admin powers. For anything
beyond a throwaway test, use the launch script, which splits the supply by a written plan and hands control away:

```bash
cd contracts
cp launch/tokenomics.example.json launch/tokenomics.json    # edit every figure and address
node scripts/launch-rehearsal.mjs                           # needs `npx hardhat node`; 41 self-checks, run it first
DOTENV_CONFIG_PATH=testnet.env node scripts/launch.mjs launch/tokenomics.json
```

What the plan controls (all of it is the owner's decision; the example numbers are placeholders):

| Setting | Meaning |
|---|---|
| `allocations[].bps` | Share of the 1,000,000,000 supply in basis points; they must add up to exactly 10,000 |
| `kind: vesting` | The share is locked in a vesting wallet: nothing before the cliff, then linear until the end |
| `timelockDelaySeconds` | Delay before any admin change on a contract owned by the timelock can run (minimum 3,600) |
| `tokenOwner` | `safe`: a multisig can pause/unpause the token. `timelock`: the same, but delayed (a pause then cannot be an emergency stop). `renounce`: nobody can ever pause it |
| `settlementOwner` | Who may rotate the voucher signer, change the fee treasury or pause settlement |
| `status` | Must be `APPROVED`, and `I_UNDERSTAND_MAINNET=yes` set, before the script will touch a mainnet chain |

Think about `tokenOwner` before a public launch: a freeze switch in the owner's hands is the first thing buyers of a traded token
look for. `renounce` removes it for good at the cost of having no emergency stop; a multisig keeps it but asks buyers to trust the
signers. The script never uses the same address for deployer and owner at the end: the deployer finishes with zero tokens and no rights.

## Token deposits (credits for COMPUTE), off by default

Set these on the API to let users deposit COMPUTE and receive credits (see ADR-0004). Without all of them it stays off.

```
TOKEN_DEPOSIT_ENABLED=true            TOKEN_DEPOSIT_ADDRESS=<platform address that receives deposits>
TOKEN_CREDITS_PER_TOKEN=<credits per 1 COMPUTE, your decision>
TOKEN_DEPOSIT_CONFIRMATIONS=12        TOKEN_DEPOSIT_MIN_TOKENS=1     TOKEN_DEPOSIT_MAX_TOKENS=100000
TOKEN_DEPOSIT_FROM_BLOCK=<the token's deployment block>
```

