/**
 * ComputeCoin — testnet deploy (ethers v6, standalone; no mainnet).
 *
 * Deploys ComputeCoinToken + ComputeSettlement to whatever RPC you point it at.
 * Reads config from ENV (never hardcode a private key):
 *   RPC_URL                  testnet JSON-RPC (e.g. BSC Testnet / Base Sepolia)
 *   PRIVATE_KEY              deployer key (TESTNET wallet only — never a real-funds key)
 *   PLATFORM_SIGNER_ADDRESS  (optional) defaults to deployer address
 *   FEE_TREASURY_ADDRESS     (optional) defaults to deployer address
 *   EXPLORER_BASE            (optional) e.g. https://testnet.bscscan.com
 *
 * Run (after `npx hardhat compile`):
 *   node scripts/deploy-testnet.mjs
 *
 * SAFETY: refuses to run if the wallet has zero balance (you need testnet gas from
 * a faucet first). This script does nothing on mainnet unless YOU point RPC_URL
 * there — don't, until after an audit + legal review.
 */
import "dotenv/config";
import { ethers } from "ethers";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dir = dirname(fileURLToPath(import.meta.url));
const root = join(__dir, "..");

function artifact(name, file) {
  const p = join(root, "artifacts", "src", `${file}.sol`, `${name}.json`);
  const a = JSON.parse(readFileSync(p, "utf8"));
  if (!a.abi || !a.bytecode) throw new Error(`Artifact ${name} missing abi/bytecode — run \`npx hardhat compile\` first`);
  return a;
}

function need(key) {
  const v = process.env[key];
  if (!v) throw new Error(`Missing env ${key} (set it in contracts/.env — see .env.example)`);
  return v;
}

async function main() {
  const RPC_URL = need("RPC_URL");
  const PRIVATE_KEY = need("PRIVATE_KEY");
  const explorer = process.env.EXPLORER_BASE?.replace(/\/$/, "") || "";

  const provider = new ethers.JsonRpcProvider(RPC_URL);
  const net = await provider.getNetwork();
  // NonceManager: two deployments back to back must not reuse a cached nonce. Without it
  // the second deploy failed with "nonce has already been used" on fast-mining chains,
  // and slow public testnets are exposed to the same race.
  const wallet = new ethers.NonceManager(new ethers.Wallet(PRIVATE_KEY, provider));
  const deployer = await wallet.getAddress();

  // Guard: don't accidentally run against Ethereum/BSC/Polygon mainnet.
  const MAINNET_CHAIN_IDS = new Set([1n, 56n, 137n, 8453n, 42161n, 10n]);
  if (MAINNET_CHAIN_IDS.has(net.chainId) && process.env.I_UNDERSTAND_MAINNET !== "yes") {
    throw new Error(
      `Refusing to deploy to mainnet chainId ${net.chainId}. This script is for TESTNET. ` +
      `Mainnet requires an audit + legal review first.`
    );
  }

  const bal = await provider.getBalance(deployer);
  console.log(`Network   : chainId ${net.chainId}`);
  console.log(`Deployer  : ${deployer}`);
  console.log(`Balance   : ${ethers.formatEther(bal)} (native testnet coin)`);
  if (bal === 0n) {
    throw new Error("Deployer balance is 0 — get testnet gas from a faucet first (see the runbook).");
  }

  const platformSigner = process.env.PLATFORM_SIGNER_ADDRESS || deployer;
  const feeTreasury = process.env.FEE_TREASURY_ADDRESS || deployer;

  // 1) Token
  const tokenArt = artifact("ComputeCoinToken", "ComputeCoinToken");
  console.log("\nDeploying ComputeCoinToken…");
  const Token = new ethers.ContractFactory(tokenArt.abi, tokenArt.bytecode, wallet);
  const token = await Token.deploy(deployer); // constructor(initialOwner)
  await token.waitForDeployment();
  const tokenAddr = await token.getAddress();
  console.log(`  ComputeCoinToken: ${tokenAddr}`);

  // 2) Settlement (escrow)
  const setArt = artifact("ComputeSettlement", "ComputeSettlement");
  console.log("Deploying ComputeSettlement…");
  const Settlement = new ethers.ContractFactory(setArt.abi, setArt.bytecode, wallet);
  const settlement = await Settlement.deploy(tokenAddr, platformSigner, feeTreasury, deployer);
  await settlement.waitForDeployment();
  const settlementAddr = await settlement.getAddress();
  console.log(`  ComputeSettlement: ${settlementAddr}`);

  const out = {
    chainId: Number(net.chainId),
    deployer,
    platformSigner,
    feeTreasury,
    ComputeCoinToken: tokenAddr,
    ComputeSettlement: settlementAddr,
    deployedAt: new Date().toISOString(),
  };
  const outPath = join(root, `deployments.${net.chainId}.json`);
  writeFileSync(outPath, JSON.stringify(out, null, 2));

  console.log("\n✅ Deployed. Saved to", outPath);
  if (explorer) {
    console.log(`\nExplorer links:`);
    console.log(`  Token:      ${explorer}/address/${tokenAddr}`);
    console.log(`  Settlement: ${explorer}/address/${settlementAddr}`);
  }
  console.log(`\nYour coin now exists on-chain. Next: add liquidity to make it tradable:`);
  console.log(`  TOKEN_ADDRESS=${tokenAddr} node scripts/add-liquidity-testnet.mjs`);
}

main().catch((e) => {
  console.error("\n❌ Deploy failed:", e.message);
  process.exit(1);
});
