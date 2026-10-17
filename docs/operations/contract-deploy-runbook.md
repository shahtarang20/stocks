# ComputeCoin & ComputeSettlement — Testnet Deployment & Verification Runbook (Workstream D)

> **Objective:** Real public testnet deployment on **Ethereum Sepolia** (and optionally Base Sepolia / BSC Testnet), cryptographic verification on Etherscan, multisig ownership setup, backend wiring, and end-to-end off-chain/on-chain settlement reconciliation.
>
> ⛔ **MAINNET AUDIT GATE:** Under no circumstances should these contracts be deployed to Ethereum Mainnet or a tradable coin launched for real funds without an independent third-party smart-contract security audit (₹8L+) and formal legal counsel. See [business2.md](../../business2.md) and [LAUNCH_PLAN.md](../../LAUNCH_PLAN.md).

---

## 1. System Architecture & Role Separation

The ComputeCoin decentralized settlement layer consists of two smart contracts:

1. **[`ComputeCoinToken.sol`](../../contracts/src/ComputeCoinToken.sol)**:
   - ERC-20 token (`COMPUTE`), 18 decimals, fixed supply of 1,000,000,000 tokens minted at genesis.
   - Includes emergency pause circuit breaker and burner functions.
2. **[`ComputeSettlement.sol`](../../contracts/src/ComputeSettlement.sol)**:
   - Non-custodial escrow and payment settlement contract.
   - Anti-squatting key derivation: escrows are keyed by `keccak256(customerAddress, jobId)`.
   - Cryptographic EIP-191 execution vouchers signed by `platformSigner`.
   - Automatic 3-way split upon settlement: Provider payout + Platform fee + Unused budget refund.
   - Dual-mode refund: early refund via platform cancellation voucher, or permissionless self-refund after deadline expiration.

### Cryptographic Roles & Key Isolation

| Role | Responsibility | Testnet Configuration | Mainnet Production Target |
| :--- | :--- | :--- | :--- |
| **Deployer** | Deploys contracts and coordinates setup | Ephemeral MetaMask testnet key | Dedicated hardware wallet |
| **Contract Owner** | Emergency pause, update platform signer, fee treasury | Deployer address or Safe multisig | **3-of-5 Gnosis Safe Multisig** |
| **Platform Signer** | Signs off-chain execution & refund vouchers | Backend test signer key | Dedicated KMS / HSM signer key |
| **Fee Treasury** | Receives protocol platform fees (e.g. 10%) | Deployer address | Cold storage multisig vault |

---

## 2. Prerequisites & Free Testnet Gas

Deploying on testnet costs **₹0 / $0**. You only need testnet gas coins from free public faucets:

### Supported Networks

| Network | Chain ID | Gas Coin | Explorer |
| :--- | :--- | :--- | :--- |
| **Ethereum Sepolia** *(Primary)* | `11155111` | SepoliaETH | [sepolia.etherscan.io](https://sepolia.etherscan.io) |
| **Base Sepolia** | `84532` | Base Sepolia ETH | [sepolia.basescan.org](https://sepolia.basescan.org) |
| **BNB Smart Chain Testnet** | `97` | tBNB | [testnet.bscscan.com](https://testnet.bscscan.com) |

### Free Faucets (Request 0.05 SepoliaETH)
- **Google Cloud Web3 Faucet** *(Instant, no login required)*:  
  👉 [https://cloud.google.com/application/web3/faucet/ethereum/sepolia](https://cloud.google.com/application/web3/faucet/ethereum/sepolia)
- **Alchemy Sepolia Faucet**:  
  👉 [https://sepoliafaucet.com](https://sepoliafaucet.com)
- **Chainlink Faucet**:  
  👉 [https://faucets.chain.link/sepolia](https://faucets.chain.link/sepolia)

---

## 3. Environment Configuration (`contracts/.env`)

1. Copy the template:
   ```bash
   cp contracts/.env.example contracts/.env
   ```

2. Open `contracts/.env` and configure your credentials:
   ```env
   # Deployer testnet private key from MetaMask (never commit this)
   DEPLOYER_PRIVATE_KEY=0xYOUR_TESTNET_PRIVATE_KEY

   # (Optional) Gnosis Safe multisig address if you want to assign ownership immediately
   MULTISIG_OWNER=

   # Platform signer public key (defaults to backend dev key or deployer if empty)
   PLATFORM_SIGNER_ADDRESS=

   # Platform treasury address (defaults to deployer if empty)
   PLATFORM_TREASURY_ADDRESS=

   # Free API key from https://etherscan.io/myapikey for contract source verification
   ETHERSCAN_API_KEY=YOUR_ETHERSCAN_API_KEY
   ```

---

## 4. Deploying to Sepolia Testnet (One Command)

From the project root:

```bash
npm run deploy:settlement -- --network sepolia
```

*(Or from `contracts/`: `npm run deploy:settlement -- --network sepolia`)*

### What the deployment script does:
1. Validates that the target is a testnet (refuses mainnet chain IDs).
2. Deploys `ComputeCoinToken.sol` (mints 1,000,000,000 COMPUTE).
3. Deploys `ComputeSettlement.sol` bound to the deployed token, platform signer, and fee treasury.
4. If `MULTISIG_OWNER` is configured, automatically transfers contract ownership to your Gnosis Safe.
5. Saves full deployment metadata to `contracts/deployments/sepolia-settlement.json`.
6. Generates a ready-to-use backend configuration file: `contracts/deployments/sepolia-backend.env`.
7. Prints clickable Etherscan block explorer links in your terminal.

---

## 5. Contract Verification on Etherscan

To get the green **"Verified"** checkmark and expose the full contract ABI and methods on Etherscan:

```bash
npm run verify:contracts -- --network sepolia
```

This script reads the recorded constructor arguments and registers the Solidity source code with Etherscan. Once complete, you will be able to read and interact with contract functions directly on Etherscan.

---

## 6. Setting Up Multisig Governance (Gnosis Safe)

For production readiness and investor diligence, contract ownership must never remain on a single developer key:

1. Go to **[https://app.safe.global](https://app.safe.global)**.
2. Connect MetaMask and select the **Sepolia** network.
3. Click **"Create Safe"** and name it `ComputeCoin-Governance-Safe`.
4. Add team wallet addresses and set the signing threshold (e.g., **2-of-3** or **3-of-5**).
5. Deploy the Safe on Sepolia (gas paid with faucet coins).
6. Copy the deployed Safe address (`0x...`).
7. Update `MULTISIG_OWNER=0xSafeAddress` in `contracts/.env` and run:
   ```bash
   npm run deploy:settlement -- --network sepolia
   ```
   *(Or transfer manually on Etherscan: `token.transferOwnership(safeAddress)` and `settlement.transferOwnership(safeAddress)`).*

---

## 7. Platform Signer & Timelock Architecture

Off-chain execution vouchers authorize payouts on-chain. To protect against key compromise:

1. **Voucher Replay & Cross-Chain Protection**:
   Each voucher signature encodes:
   ```solidity
   bytes32 messageHash = keccak256(
       abi.encodePacked(customer, jobId, finalCost, platformFee, block.chainid, address(this))
   );
   ```
   - Bound to `block.chainid` (cannot be replayed on mainnet or another testnet).
   - Bound to `address(this)` (cannot be replayed on any other contract).
   - Bound to `customer` and `jobId` (cannot be applied to another user's balance).

2. **Signer Rotation**:
   The contract owner (Multisig Safe) can update the signer at any time via:
   ```solidity
   settlement.setPlatformSigner(newSignerAddress);
   ```

3. **Timelock Recommendation**:
   For mainnet, place contract ownership behind an OpenZeppelin `TimelockController` with a 48-hour delay. Any governance actions (signer rotation, pause, fee change) will be publicly queued for 48 hours before execution, preventing sudden unauthorized modifications.

---

## 8. Wiring Deployed Contracts to the Backend (`apps/api`)

Once deployed, copy the generated environment variables from `contracts/deployments/sepolia-backend.env` into `apps/api/.env`:

```env
# Phase 10 — On-chain settlement & smart contracts
SETTLEMENT_ENABLED=true
SETTLEMENT_CHAIN_ID=11155111
SETTLEMENT_CONTRACT_ADDRESS=0xYourSepoliaSettlementAddress
COMPUTE_TOKEN_ADDRESS=0xYourSepoliaTokenAddress
PLATFORM_TREASURY_ADDRESS=0xYourTreasuryAddress
PLATFORM_SIGNER_PRIVATE_KEY=0xYourPlatformSignerKey
```

### Verify Backend Connection
Start the API and inspect the public contract discovery endpoint:

```bash
curl http://localhost:8000/v1/settlement/contracts
```

**Expected JSON Response:**
```json
{
  "chain_id": 11155111,
  "network_name": "Sepolia Testnet",
  "token_address": "0x...",
  "token_symbol": "COMPUTE",
  "settlement_contract_address": "0x...",
  "platform_signer_address": "0x...",
  "treasury_address": "0x...",
  "is_paused": false
}
```

---

## 9. End-to-End Testnet Lifecycle & Reconciliation Verification

To test the complete lifecycle against Sepolia testnet or local dev node:

```bash
# Against Sepolia Testnet:
npm run testnet:e2e -- --network sepolia

# Against Local Node:
npm run testnet:e2e -- --network localhost
```

### The E2E script executes and asserts:
1. **Budget Lock**: Customer calls `lockBudget(jobId, provider, maxCost, deadline)`.
2. **Cryptographic Voucher**: Platform issues EIP-191 signed voucher with exact compute runtime fee.
3. **Settlement**: `settleJob()` executes on-chain and verifies:
   - Provider receives payout (`finalCost - platformFee`).
   - Platform fee treasury receives `platformFee`.
   - Customer receives refund of unspent budget (`maxCost - finalCost`).
4. **Early Refund Path**: Platform issues cancellation voucher; `refundJob()` returns 100% of tokens to customer.
5. **Replay & Squatting Protection**: Verifies that replaying an old voucher fails, and verifying that another user cannot squat a job ID.
6. **Financial Invariants**: Confirms zero discrepancy ($0 wei loss) between off-chain database ledger and on-chain ERC-20 balances.

---

## 10. Pre-Mainnet Security & Audit Gate

| Milestone | Requirement | Status |
| :--- | :--- | :--- |
| **Workstream A** | Cookie Auth, Anti-Squatting, CSRF Double-Submit | ✅ Complete |
| **Workstream B** | GPU Worker Sandbox, Digest-Pinned Model Registry | ✅ Complete |
| **Workstream C** | Stripe Payment Rail & Idempotent Webhooks | ✅ Complete |
| **Workstream D** | Sepolia Testnet Deployment & E2E Reconciliation | ✅ Complete |
| **External Security Audit** | Formal review by CertiK / OpenZeppelin / Halborn | ⏳ Gated (Paid via platform revenue) |
| **Legal Opinion** | Utility token classification & tax compliance | ⏳ Gated |
| **Mainnet Launch** | Multi-million dollar liquidity + Mainnet deploy | ⛔ **BLOCKED until Audit & Legal Clear** |
