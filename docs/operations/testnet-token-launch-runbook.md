# ComputeCoin (COMPUTE) — Testnet Token Launch Runbook

> **Goal:** Deploy the real `ComputeCoinToken.sol` ERC-20 contract to a public testnet for **₹0 / $0**, receive the 1 Billion `COMPUTE` genesis supply into your MetaMask wallet, view your live token on a public block explorer (Etherscan / Basescan / BscScan), and create a testnet decentralized exchange (DEX) liquidity pool so people can trade it with test money.

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

To let people buy and sell `COMPUTE` using test coins on Uniswap or PancakeSwap, create a liquidity pool:

```bash
# From contracts/ directory:
npm run create:pool -- --network sepolia
```

### What this script does:
1. Reads your deployed token address from `deployments/sepolia-token.json`.
2. Approves the Uniswap / PancakeSwap router to transfer `COMPUTE`.
3. Calls `addLiquidityETH` with **10,000,000 COMPUTE** paired with **0.005 test ETH**.
4. Discovers the newly created **LP Pair Contract address**.
5. Saves the pool receipt to `contracts/deployments/sepolia-liquidity-pool.json`.
6. Prints the **direct Uniswap / PancakeSwap Swap URL**!

Anyone can now open the testnet DEX link and swap test ETH for `COMPUTE`!

---

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
# 1. Setup env
cd contracts
cp .env.example .env
# (edit .env with your private key)

# 2. Deploy token
npm run deploy:token -- --network sepolia

# 3. Create liquidity pool
npm run create:pool -- --network sepolia
```
