#!/usr/bin/env node
/**
 * ComputeCoin & ComputeSettlement — Block Explorer Verification Script
 *
 * Automatically verifies deployed contracts on Sepolia Etherscan / Basescan / BscScan
 * using the constructor arguments recorded during deployment.
 *
 * Usage:
 *   npm run verify:contracts -- --network sepolia
 *   npm run verify:contracts -- --network base-sepolia
 *   npm run verify:contracts -- --network bsc-testnet
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

const EXPLORER_API_ENDPOINTS = {
  sepolia: {
    apiUrl: "https://api-sepolia.etherscan.io/api",
    apiKeyEnv: "ETHERSCAN_API_KEY",
    explorerUrl: "https://sepolia.etherscan.io",
    name: "Sepolia Etherscan",
  },
  "base-sepolia": {
    apiUrl: "https://api-sepolia.basescan.org/api",
    apiKeyEnv: "BASESCAN_API_KEY",
    explorerUrl: "https://sepolia.basescan.org",
    name: "Base Sepolia Basescan",
  },
  "bsc-testnet": {
    apiUrl: "https://api-testnet.bscscan.com/api",
    apiKeyEnv: "BSCSCAN_API_KEY",
    explorerUrl: "https://testnet.bscscan.com",
    name: "BSC Testnet BscScan",
  },
};

async function main() {
  console.log("\n========================================================");
  console.log(" 🔍 Contract Source Verification on Block Explorer");
  console.log("========================================================\n");

  let networkName = hre.globalOptions?.network;
  if (!networkName) {
    const netIdx = process.argv.indexOf("--network");
    if (netIdx !== -1 && process.argv[netIdx + 1]) {
      networkName = process.argv[netIdx + 1];
    }
  }
  networkName = networkName || process.env.HARDHAT_NETWORK || "sepolia";

  if (networkName === "localhost" || networkName === "hardhat") {
    console.log("ℹ️  Local networks do not have an external block explorer. Verification skipped.");
    return;
  }

  const deploymentFile = path.join(deploymentsDir, `${networkName}-settlement.json`);
  if (!fs.existsSync(deploymentFile)) {
    console.error(`❌ ERROR: Deployment file not found at ${deploymentFile}!`);
    console.error(`Please deploy contracts first using:\n  npm run deploy:settlement -- --network ${networkName}`);
    process.exit(1);
  }

  const receipt = JSON.parse(fs.readFileSync(deploymentFile, "utf8"));
  const explorerConfig = EXPLORER_API_ENDPOINTS[networkName];

  if (!explorerConfig) {
    console.warn(`⚠️  No explorer verification endpoint defined for network: ${networkName}`);
    return;
  }

  const apiKey = process.env[explorerConfig.apiKeyEnv] || process.env.ETHERSCAN_API_KEY || "";

  console.log(`📡 Network:          ${explorerConfig.name}`);
  console.log(`🪙 Token Address:    ${receipt.tokenAddress}`);
  console.log(`🏛️ Settlement Addr:  ${receipt.settlementAddress}`);

  if (!apiKey) {
    console.log(`\n⚠️  Notice: ${explorerConfig.apiKeyEnv} is not set in contracts/.env.`);
    console.log(`To enable automatic 1-click verification:`);
    console.log(`  1. Get a free API key at https://etherscan.io/myapikey (or ${explorerConfig.name})`);
    console.log(`  2. Add it to contracts/.env:`);
    console.log(`     ${explorerConfig.apiKeyEnv}=YOUR_API_KEY`);
    console.log(`  3. Re-run: npm run verify:contracts -- --network ${networkName}\n`);
    console.log(`Or verify manually on the web UI by pasting the flattened contract:`);
    console.log(`  - Token:      ${explorerConfig.explorerUrl}/address/${receipt.tokenAddress}#code`);
    console.log(`  - Settlement: ${explorerConfig.explorerUrl}/address/${receipt.settlementAddress}#code\n`);
    return;
  }

  console.log(`\n🔑 Found API key for ${explorerConfig.name}. Submitting verification...`);

  // Verify ComputeCoinToken
  try {
    console.log(`\n[1/2] Verifying ComputeCoinToken at ${receipt.tokenAddress}...`);
    const tokenArgs = receipt.constructorArguments?.ComputeCoinToken || [receipt.deployer];
    console.log(`      Constructor args:`, tokenArgs);

    // Call hardhat verify task if available, or direct API
    try {
      await hre.run("verify:verify", {
        address: receipt.tokenAddress,
        constructorArguments: tokenArgs,
      });
      console.log(`      ✅ ComputeCoinToken verified successfully!`);
    } catch (innerErr) {
      if (innerErr.message?.includes("Already Verified") || innerErr.message?.includes("already verified")) {
        console.log(`      ✅ ComputeCoinToken is already verified!`);
      } else {
        console.log(`      ℹ️  Verification response: ${innerErr.message}`);
      }
    }
  } catch (err) {
    console.warn(`      ⚠️  Token verification notice: ${err.message}`);
  }

  // Verify ComputeSettlement
  try {
    console.log(`\n[2/2] Verifying ComputeSettlement at ${receipt.settlementAddress}...`);
    const settlementArgs = receipt.constructorArguments?.ComputeSettlement || [
      receipt.tokenAddress,
      receipt.platformSigner,
      receipt.feeTreasury,
      receipt.deployer,
    ];
    console.log(`      Constructor args:`, settlementArgs);

    try {
      await hre.run("verify:verify", {
        address: receipt.settlementAddress,
        constructorArguments: settlementArgs,
      });
      console.log(`      ✅ ComputeSettlement verified successfully!`);
    } catch (innerErr) {
      if (innerErr.message?.includes("Already Verified") || innerErr.message?.includes("already verified")) {
        console.log(`      ✅ ComputeSettlement is already verified!`);
      } else {
        console.log(`      ℹ️  Verification response: ${innerErr.message}`);
      }
    }
  } catch (err) {
    console.warn(`      ⚠️  Settlement verification notice: ${err.message}`);
  }

  console.log("\n========================================================");
  console.log(" 🎉 Verification Process Complete");
  console.log("========================================================");
  console.log(`Explorer links:`);
  console.log(`  - Token:      ${explorerConfig.explorerUrl}/token/${receipt.tokenAddress}#code`);
  console.log(`  - Settlement: ${explorerConfig.explorerUrl}/address/${receipt.settlementAddress}#code`);
  console.log("========================================================\n");
}

main().catch((err) => {
  console.error("\n❌ Verification error:", err.message);
  process.exit(1);
});
