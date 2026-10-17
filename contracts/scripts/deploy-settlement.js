#!/usr/bin/env node
/**
 * ComputeCoin & ComputeSettlement — Sepolia & Testnet Deployment Script
 *
 * Deploys:
 *   1. ComputeCoinToken.sol (ERC-20, 1 Billion COMPUTE max supply)
 *   2. ComputeSettlement.sol (Escrow, Voucher Verification, Nonce/Anti-Squatting)
 *
 * Configurable via contracts/.env:
 *   - DEPLOYER_PRIVATE_KEY: Testnet deployer wallet
 *   - MULTISIG_OWNER / SAFE_ADDRESS: (Optional) Gnosis Safe address for contract ownership
 *   - PLATFORM_SIGNER_PRIVATE_KEY / PLATFORM_SIGNER_ADDRESS: Public key authorized to sign execution vouchers
 *   - PLATFORM_TREASURY_ADDRESS: Fee recipient
 *   - TOKEN_ADDRESS: (Optional) Reuse existing token deployment instead of deploying fresh
 *
 * Usage:
 *   npm run deploy:settlement -- --network sepolia
 *   npm run deploy:settlement -- --network base-sepolia
 *   npm run deploy:settlement -- --network bsc-testnet
 *   npm run deploy:settlement -- --network localhost
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import hre from "hardhat";
import { ethers } from "ethers";
import "dotenv/config";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const contractsRoot = path.join(__dirname, "..");
const deploymentsDir = path.join(contractsRoot, "deployments");

const NETWORK_METADATA = {
  sepolia: {
    name: "Ethereum Sepolia Testnet",
    chainId: 11155111,
    currency: "SepoliaETH",
    explorer: "https://sepolia.etherscan.io",
    faucets: [
      "https://cloud.google.com/application/web3/faucet/ethereum/sepolia",
      "https://sepoliafaucet.com",
      "https://faucets.chain.link/sepolia",
    ],
  },
  "base-sepolia": {
    name: "Base Sepolia Testnet",
    chainId: 84532,
    currency: "ETH",
    explorer: "https://sepolia.basescan.org",
    faucets: [
      "https://www.coinbase.com/faucets/base-ethereum-sepolia-faucet",
      "https://faucets.chain.link/base-sepolia",
    ],
  },
  "bsc-testnet": {
    name: "BNB Smart Chain Testnet",
    chainId: 97,
    currency: "tBNB",
    explorer: "https://testnet.bscscan.com",
    faucets: ["https://www.bnbchain.org/en/testnet-faucet"],
  },
  localhost: {
    name: "Local Hardhat Node",
    chainId: 31337,
    currency: "ETH",
    explorer: "http://localhost:8545",
    faucets: ["http://127.0.0.1:8545 (Local dev node)"],
  },
};

function readArtifact(contractName) {
  const artifactPath = path.join(
    contractsRoot,
    "artifacts",
    "src",
    `${contractName}.sol`,
    `${contractName}.json`
  );

  if (!fs.existsSync(artifactPath)) {
    throw new Error(
      `Artifact not found for ${contractName} at ${artifactPath}. Run 'npm run compile' first.`
    );
  }

  return JSON.parse(fs.readFileSync(artifactPath, "utf8"));
}

async function resolveRpcUrl(networkName) {
  const netConfig = hre.config?.networks?.[networkName];
  if (netConfig && netConfig.url) {
    if (typeof netConfig.url._getRawValue === "function") {
      return await netConfig.url._getRawValue();
    }
    if (typeof netConfig.url === "string") {
      return netConfig.url;
    }
  }

  if (networkName === "sepolia") return process.env.SEPOLIA_RPC_URL || "https://rpc.sepolia.org";
  if (networkName === "base-sepolia") return process.env.BASE_SEPOLIA_RPC_URL || "https://sepolia.base.org";
  if (networkName === "bsc-testnet") return process.env.BSC_TESTNET_RPC_URL || "https://data-seed-prebsc-1-s1.binance.org:8545";
  return "http://127.0.0.1:8545";
}

async function main() {
  console.log("\n========================================================");
  console.log(" 🏛️  ComputeCoin & ComputeSettlement — Testnet Deployment");
  console.log("========================================================\n");

  // 1. Determine target network
  let networkName = hre.globalOptions?.network;
  if (!networkName) {
    const netIdx = process.argv.indexOf("--network");
    if (netIdx !== -1 && process.argv[netIdx + 1]) {
      networkName = process.argv[netIdx + 1];
    }
  }
  networkName = networkName || process.env.HARDHAT_NETWORK || "sepolia";

  const meta = NETWORK_METADATA[networkName] || {
    name: networkName,
    chainId: 0,
    currency: "ETH",
    explorer: "",
    faucets: [],
  };

  console.log(`📡 Target Network: ${meta.name}`);

  // 2. Resolve deployer private key & RPC
  let privateKey = process.env.DEPLOYER_PRIVATE_KEY || process.env.PRIVATE_KEY;
  const rpcUrl = await resolveRpcUrl(networkName);

  if (!privateKey && (networkName === "localhost" || networkName === "hardhat")) {
    privateKey = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
    console.log("ℹ️  Using default dev account for local network.");
  }

  if (!privateKey) {
    console.error("\n❌ ERROR: DEPLOYER_PRIVATE_KEY is missing in contracts/.env!");
    console.error("\nTo fix this:");
    console.error("  1. Copy the example file:  cp contracts/.env.example contracts/.env");
    console.error("  2. Open contracts/.env and paste your testnet private key:");
    console.error("     DEPLOYER_PRIVATE_KEY=0xYourPrivateKeyHere");
    console.error(`  3. Run this script again:  npm run deploy:settlement -- --network ${networkName}\n`);
    if (meta.faucets.length > 0) {
      console.error(`Free testnet faucets for ${meta.name}:`);
      meta.faucets.forEach((f) => console.error("   - " + f));
      console.error("");
    }
    process.exit(1);
  }

  if (!privateKey.startsWith("0x")) {
    privateKey = "0x" + privateKey;
  }

  // 3. Connect to network
  const provider = new ethers.JsonRpcProvider(rpcUrl);
  const net = await provider.getNetwork();
  const chainId = Number(net.chainId);
  console.log(`🔗 Connected to Chain ID: ${chainId} via ${rpcUrl}`);

  // Guard against accidental mainnet deployment
  const MAINNET_CHAIN_IDS = new Set([1, 56, 137, 8453, 42161, 10]);
  if (MAINNET_CHAIN_IDS.has(chainId) && process.env.I_UNDERSTAND_MAINNET !== "yes") {
    console.error("\n🛑 SAFETY CHECK FAILED: Refusing to deploy to mainnet without explicit confirmation!");
    console.error("Workstream D is strictly scoped for public testnet (Sepolia).");
    console.error("Mainnet launch requires an external security audit + legal clearance.\n");
    process.exit(1);
  }

  const wallet = new ethers.Wallet(privateKey, provider);
  const deployerAddress = await wallet.getAddress();
  const balance = await provider.getBalance(deployerAddress);

  console.log(`👤 Deployer Address: ${deployerAddress}`);
  console.log(`💰 Gas Balance:     ${ethers.formatEther(balance)} ${meta.currency}`);

  if (balance === 0n) {
    console.error(`\n❌ ERROR: Deployer balance is 0 ${meta.currency}!`);
    console.error("You need testnet gas to deploy contracts.");
    if (meta.faucets.length > 0) {
      console.error(`\nGrab free test coins for ${meta.name}:`);
      meta.faucets.forEach((f) => console.error("   - " + f));
    }
    process.exit(1);
  }

  // 4. Resolve Protocol Addresses
  const multisigOwner = process.env.MULTISIG_OWNER || process.env.SAFE_ADDRESS || "";
  let platformSigner = process.env.PLATFORM_SIGNER_ADDRESS || "";
  if (!platformSigner && process.env.PLATFORM_SIGNER_PRIVATE_KEY) {
    try {
      const signerWallet = new ethers.Wallet(process.env.PLATFORM_SIGNER_PRIVATE_KEY);
      platformSigner = signerWallet.address;
    } catch {
      // ignore
    }
  }
  if (!platformSigner) {
    platformSigner = deployerAddress;
  }

  const feeTreasury = process.env.PLATFORM_TREASURY_ADDRESS || process.env.FEE_TREASURY_ADDRESS || deployerAddress;

  console.log("\n📋 Protocol Roles Configuration:");
  console.log(`   - Initial Deployer:  ${deployerAddress}`);
  console.log(`   - Platform Signer:   ${platformSigner}`);
  console.log(`   - Fee Treasury:      ${feeTreasury}`);
  if (multisigOwner) {
    console.log(`   - Multisig Owner:    ${multisigOwner} (Safe)`);
  } else {
    console.log(`   - Multisig Owner:    Not set (Deployer retains admin until Safe is configured)`);
  }

  // Ensure deployments directory exists
  if (!fs.existsSync(deploymentsDir)) {
    fs.mkdirSync(deploymentsDir, { recursive: true });
  }

  const txHashes = {};

  let currentNonce = parseInt(
    await provider.send("eth_getTransactionCount", [deployerAddress, "latest"]),
    16
  );

  function nextNonce() {
    return currentNonce++;
  }

  // 5. Deploy or Resolve ComputeCoinToken
  let tokenAddress = process.env.TOKEN_ADDRESS || "";
  let tokenDeployTx = "";

  const tokenArtifact = readArtifact("ComputeCoinToken");

  if (tokenAddress && ethers.isAddress(tokenAddress)) {
    console.log(`\n♻️  Reusing existing ComputeCoinToken at: ${tokenAddress}`);
  } else {
    console.log("\n🚀 [1/2] Deploying ComputeCoinToken (COMPUTE)...");
    const TokenFactory = new ethers.ContractFactory(tokenArtifact.abi, tokenArtifact.bytecode, wallet);
    const tokenContract = await TokenFactory.deploy(deployerAddress, {
      nonce: await nextNonce(),
    });
    console.log(`   Transaction submitted: ${tokenContract.deploymentTransaction().hash}`);
    await tokenContract.waitForDeployment();
    tokenAddress = await tokenContract.getAddress();
    tokenDeployTx = tokenContract.deploymentTransaction().hash;
    txHashes.tokenDeploy = tokenDeployTx;
    console.log(`   ✅ ComputeCoinToken deployed at: ${tokenAddress}`);
  }

  // 6. Deploy ComputeSettlement
  console.log("\n🚀 [2/2] Deploying ComputeSettlement...");
  const settlementArtifact = readArtifact("ComputeSettlement");
  const SettlementFactory = new ethers.ContractFactory(settlementArtifact.abi, settlementArtifact.bytecode, wallet);

  // Constructor args: (token, platformSigner, feeTreasury, initialOwner)
  const settlementArgs = [tokenAddress, platformSigner, feeTreasury, deployerAddress];
  const settlementContract = await SettlementFactory.deploy(...settlementArgs, {
    nonce: await nextNonce(),
  });
  console.log(`   Transaction submitted: ${settlementContract.deploymentTransaction().hash}`);
  await settlementContract.waitForDeployment();
  const settlementAddress = await settlementContract.getAddress();
  const settlementDeployTx = settlementContract.deploymentTransaction().hash;
  txHashes.settlementDeploy = settlementDeployTx;
  console.log(`   ✅ ComputeSettlement deployed at: ${settlementAddress}`);

  // 7. Transfer Ownership to Multisig if configured
  let finalOwner = deployerAddress;
  if (multisigOwner && ethers.isAddress(multisigOwner) && multisigOwner.toLowerCase() !== deployerAddress.toLowerCase()) {
    console.log(`\n🔐 Transferring contract ownership to Multisig: ${multisigOwner}...`);

    try {
      const tokenInstance = new ethers.Contract(tokenAddress, tokenArtifact.abi, wallet);
      const tokenOwner = await tokenInstance.owner();
      if (tokenOwner.toLowerCase() === deployerAddress.toLowerCase()) {
        const tx1 = await tokenInstance.transferOwnership(multisigOwner, {
          nonce: await nextNonce(),
        });
        await tx1.wait();
        txHashes.transferTokenOwnership = tx1.hash;
        console.log(`   ✅ ComputeCoinToken ownership transferred to ${multisigOwner} (tx: ${tx1.hash})`);
      }
    } catch (err) {
      console.warn(`   ⚠️  Could not transfer ComputeCoinToken ownership: ${err.message}`);
    }

    try {
      const tx2 = await settlementContract.transferOwnership(multisigOwner, {
        nonce: await nextNonce(),
      });
      await tx2.wait();
      txHashes.transferSettlementOwnership = tx2.hash;
      console.log(`   ✅ ComputeSettlement ownership transferred to ${multisigOwner} (tx: ${tx2.hash})`);
      finalOwner = multisigOwner;
    } catch (err) {
      console.warn(`   ⚠️  Could not transfer ComputeSettlement ownership: ${err.message}`);
    }
  }

  // 8. Save Deployment JSON
  const deploymentReceipt = {
    network: networkName,
    chainId,
    deployer: deployerAddress,
    owner: finalOwner,
    isMultisig: Boolean(multisigOwner && ethers.isAddress(multisigOwner)),
    tokenAddress,
    settlementAddress,
    platformSigner,
    feeTreasury,
    transactionHashes: txHashes,
    constructorArguments: {
      ComputeCoinToken: [deployerAddress],
      ComputeSettlement: settlementArgs,
    },
    explorerUrls: {
      token: meta.explorer ? `${meta.explorer}/token/${tokenAddress}` : "",
      settlement: meta.explorer ? `${meta.explorer}/address/${settlementAddress}` : "",
    },
    deployedAt: new Date().toISOString(),
  };

  const deploymentFile = path.join(deploymentsDir, `${networkName}-settlement.json`);
  fs.writeFileSync(deploymentFile, JSON.stringify(deploymentReceipt, null, 2));

  // Also create a convenience copy if sepolia
  if (networkName === "sepolia") {
    fs.writeFileSync(
      path.join(deploymentsDir, "sepolia-settlement.json"),
      JSON.stringify(deploymentReceipt, null, 2)
    );
  }

  // 9. Generate Backend Config Snippet
  const backendEnvContent = [
    `# ==============================================================================`,
    `# ComputeCoin Backend Settlement Configuration (${meta.name})`,
    `# Generated at: ${deploymentReceipt.deployedAt}`,
    `# ==============================================================================`,
    `SETTLEMENT_ENABLED=true`,
    `SETTLEMENT_CHAIN_ID=${chainId}`,
    `SETTLEMENT_CONTRACT_ADDRESS=${settlementAddress}`,
    `COMPUTE_TOKEN_ADDRESS=${tokenAddress}`,
    `PLATFORM_TREASURY_ADDRESS=${feeTreasury}`,
    `# Set your production/staging platform signer key below:`,
    process.env.PLATFORM_SIGNER_PRIVATE_KEY
      ? `PLATFORM_SIGNER_PRIVATE_KEY=${process.env.PLATFORM_SIGNER_PRIVATE_KEY}`
      : `# PLATFORM_SIGNER_PRIVATE_KEY=0x...`,
    ``,
  ].join("\n");

  const backendEnvFile = path.join(deploymentsDir, `${networkName}-backend.env`);
  fs.writeFileSync(backendEnvFile, backendEnvContent);

  // 10. Summary Output
  console.log("\n========================================================");
  console.log(" 🎉 Deployment Complete & Verified!");
  console.log("========================================================");
  console.log(`\n📄 Deployment artifact: ${deploymentFile}`);
  console.log(`🔧 Backend config:     ${backendEnvFile}`);

  if (meta.explorer) {
    console.log("\n🔍 Live Block Explorer Links:");
    console.log(`   - Token (COMPUTE):   ${meta.explorer}/token/${tokenAddress}`);
    console.log(`   - ComputeSettlement: ${meta.explorer}/address/${settlementAddress}`);
  }

  console.log("\n⚙️  To wire into apps/api/.env:");
  console.log("--------------------------------------------------------");
  console.log(`SETTLEMENT_ENABLED=true`);
  console.log(`SETTLEMENT_CHAIN_ID=${chainId}`);
  console.log(`SETTLEMENT_CONTRACT_ADDRESS=${settlementAddress}`);
  console.log(`COMPUTE_TOKEN_ADDRESS=${tokenAddress}`);
  console.log(`PLATFORM_TREASURY_ADDRESS=${feeTreasury}`);
  console.log("--------------------------------------------------------");

  console.log("\n📋 Next Steps in Workstream D:");
  console.log(`  1. Verify source code on Etherscan:`);
  console.log(`     npm run verify:contracts -- --network ${networkName}`);
  console.log(`  2. Run the end-to-end testnet settlement lifecycle:`);
  console.log(`     npm run testnet:e2e -- --network ${networkName}`);
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("\n❌ Deployment failed:", error);
  process.exit(1);
});
