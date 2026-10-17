#!/usr/bin/env node
/**
 * ComputeCoinToken (COMPUTE) — One-Command Testnet Deployer
 *
 * Deploys ComputeCoinToken.sol, mints the initial 1,000,000,000 COMPUTE supply to
 * the deployer (or INITIAL_OWNER), records the deployment artifact, and provides
 * direct links to view the live token on the block explorer and add it to MetaMask.
 *
 * Usage:
 *   npx hardhat run scripts/deploy-token.js --network sepolia
 *   npx hardhat run scripts/deploy-token.js --network base-sepolia
 *   npx hardhat run scripts/deploy-token.js --network bsc-testnet
 *   npm run deploy:token -- --network sepolia
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import hre from "hardhat";
import { ethers } from "ethers";
import "dotenv/config";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Known block explorers and faucets
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
    faucets: [],
  },
};

async function resolveRpcUrl(networkName) {
  const envKey = `${networkName.toUpperCase().replace(/-/g, "_")}_RPC_URL`;
  if (process.env[envKey]) return process.env[envKey];

  const netConfig = hre.config.networks[networkName];
  if (netConfig && netConfig.url) {
    if (typeof netConfig.url._getRawValue === "function") {
      return await netConfig.url._getRawValue();
    }
    if (typeof netConfig.url === "string") {
      return netConfig.url;
    }
  }

  if (networkName === "sepolia") return "https://rpc.sepolia.org";
  if (networkName === "base-sepolia") return "https://sepolia.base.org";
  if (networkName === "bsc-testnet") return "https://data-seed-prebsc-1-s1.binance.org:8545";
  return "http://127.0.0.1:8545";
}

async function main() {
  console.log("\n========================================================");
  console.log(" 🚀 ComputeCoin (COMPUTE) — Testnet Token Launch");
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
    chainId: (await hre.network?.provider?.send?.("eth_chainId", []).catch(() => 0)) || 0,
    currency: "ETH",
    explorer: "",
    faucets: [],
  };

  console.log(`📡 Target Network: ${meta.name} (Chain ID: ${meta.chainId})`);

  // 2. Resolve deployer private key & RPC
  let privateKey = process.env.DEPLOYER_PRIVATE_KEY || process.env.PRIVATE_KEY;
  let rpcUrl = await resolveRpcUrl(networkName);

  if (!privateKey && (networkName === "localhost" || networkName === "hardhat")) {
    // Dev fallback: use standard Hardhat dev account #0
    privateKey = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
    console.log("ℹ️  Using default dev account for local network.");
  }

  if (!privateKey) {
    console.error("\n❌ ERROR: DEPLOYER_PRIVATE_KEY is missing in contracts/.env!");
    console.error("\nTo fix this:");
    console.error("  1. Copy the example file:  cp contracts/.env.example contracts/.env");
    console.error("  2. Open contracts/.env and paste your testnet private key:");
    console.error("     DEPLOYER_PRIVATE_KEY=0xYourPrivateKeyHere");
    console.error("  3. Run this script again:  npm run deploy:token -- --network " + networkName + "\n");
    if (meta.faucets.length > 0) {
      console.error("Free testnet faucets for " + meta.name + ":");
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
  const deployer = new ethers.Wallet(privateKey, provider);
  console.log(`👛 Deployer Address: ${deployer.address}`);

  // 4. Check native balance for gas
  const balance = await provider.getBalance(deployer.address);
  const formattedBalance = ethers.formatEther(balance);
  console.log(`💰 Gas Balance:     ${formattedBalance} ${meta.currency}`);

  if (balance === 0n) {
    console.error(`\n❌ ERROR: Deployer wallet has 0 ${meta.currency}!`);
    console.error(`You need a tiny amount of testnet gas to deploy ComputeCoinToken.`);
    if (meta.faucets.length > 0) {
      console.error(`\nGet free testnet coins here (paste your address ${deployer.address}):`);
      meta.faucets.forEach((f) => console.error(`   👉 ${f}`));
    }
    console.error("");
    process.exit(1);
  }

  // 5. Determine initial owner (receives genesis tokens)
  const initialOwner = process.env.INITIAL_OWNER || deployer.address;
  console.log(`👑 Genesis Token Recipient (Initial Owner): ${initialOwner}`);

  // 6. Load artifact and deploy
  console.log("\n📦 Loading ComputeCoinToken contract artifact...");
  const artifact = await hre.artifacts.readArtifact("ComputeCoinToken");
  const factory = new ethers.ContractFactory(artifact.abi, artifact.bytecode, deployer);

  console.log("🚀 Broadcasting deployment transaction to blockchain...");
  const tokenContract = await factory.deploy(initialOwner);
  const deployTx = tokenContract.deploymentTransaction();
  console.log(`⏳ Tx Hash: ${deployTx.hash}`);
  console.log("⏳ Waiting for block confirmation...");

  await tokenContract.waitForDeployment();
  const tokenAddress = await tokenContract.getAddress();

  // Verify initial state
  const name = await tokenContract.name();
  const symbol = await tokenContract.symbol();
  const decimals = await tokenContract.decimals();
  const totalSupply = await tokenContract.totalSupply();
  const formattedSupply = Number(ethers.formatUnits(totalSupply, decimals)).toLocaleString();

  const explorerTokenUrl = meta.explorer ? `${meta.explorer}/token/${tokenAddress}` : "";
  const explorerTxUrl = meta.explorer ? `${meta.explorer}/tx/${deployTx.hash}` : "";

  console.log("\n========================================================");
  console.log(" 🎉 ComputeCoin (COMPUTE) SUCCESSFULLY DEPLOYED!");
  console.log("========================================================");
  console.log(` Token Name:      ${name}`);
  console.log(` Token Symbol:    ${symbol}`);
  console.log(` Decimals:        ${decimals}`);
  console.log(` Total Supply:    ${formattedSupply} COMPUTE`);
  console.log(` Contract Address: ${tokenAddress}`);
  console.log(` Owner Wallet:    ${initialOwner}`);
  if (explorerTokenUrl) {
    console.log(`\n 🔍 Live Block Explorer URL:`);
    console.log(`    👉 ${explorerTokenUrl}`);
    console.log(` 📜 Deployment Transaction:`);
    console.log(`    👉 ${explorerTxUrl}`);
  }
  console.log("========================================================\n");

  // 7. Save deployment record to disk
  const deploymentsDir = path.join(__dirname, "..", "deployments");
  if (!fs.existsSync(deploymentsDir)) {
    fs.mkdirSync(deploymentsDir, { recursive: true });
  }

  const receipt = {
    network: networkName,
    chainId: meta.chainId,
    contractName: "ComputeCoinToken",
    tokenAddress,
    tokenName: name,
    tokenSymbol: symbol,
    decimals: Number(decimals),
    totalSupply: totalSupply.toString(),
    formattedSupply,
    initialOwner,
    deployer: deployer.address,
    transactionHash: deployTx.hash,
    explorerTokenUrl,
    explorerTxUrl,
    deployedAt: new Date().toISOString(),
  };

  const receiptPath = path.join(deploymentsDir, `${networkName}-token.json`);
  fs.writeFileSync(receiptPath, JSON.stringify(receipt, null, 2));
  console.log(`💾 Saved deployment receipt to: ${path.relative(process.cwd(), receiptPath)}`);

  // 8. Next steps guidance
  console.log("\n📋 HOW TO SEE IT IN METAMASK:");
  console.log("  1. Open MetaMask and switch network to: " + meta.name);
  console.log("  2. Click 'Import tokens' -> 'Custom token'");
  console.log("  3. Token Contract Address: " + tokenAddress);
  console.log("  4. Token Symbol: COMPUTE (Decimals: 18)");
  console.log("  5. Click 'Add Custom Token' — your " + formattedSupply + " COMPUTE will appear!\n");

  console.log("🌊 NEXT STEP: CREATE A TESTNET LIQUIDITY POOL:");
  console.log("  Run the liquidity pool creation script to make your token tradable:");
  console.log(`  👉 npm run create:pool -- --network ${networkName}\n`);
}

main().catch((error) => {
  console.error("\n❌ Deployment failed:", error);
  process.exit(1);
});
