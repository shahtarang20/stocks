#!/usr/bin/env node
/**
 * ComputeCoin (COMPUTE) — Testnet Liquidity Pool Creator
 *
 * Creates a Uniswap V2 / PancakeSwap V2 liquidity pool on testnet, pairing
 * COMPUTE with test ETH / BNB to make the token tradable with fake money.
 *
 * Usage:
 *   npx hardhat run scripts/create-liquidity-pool.js --network sepolia
 *   npx hardhat run scripts/create-liquidity-pool.js --network base-sepolia
 *   npx hardhat run scripts/create-liquidity-pool.js --network bsc-testnet
 *   npm run create:pool -- --network sepolia
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import hre from "hardhat";
import { ethers } from "ethers";
import "dotenv/config";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Known DEX Routers and Factories on testnets
const DEX_CONFIG = {
  sepolia: {
    name: "Ethereum Sepolia (Uniswap V2)",
    router: "0xeE567Fe12f21f393679983795B0235E4767BEA28",
    factory: "0xF62c03E08ada871A0bEb309762E260a7a6a880E6",
    weth: "0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9",
    currency: "SepoliaETH",
    explorer: "https://sepolia.etherscan.io",
    swapUrl: (token) => `https://app.uniswap.org/explore/tokens/sepolia/${token}`,
  },
  "base-sepolia": {
    name: "Base Sepolia (Uniswap V2 on Base)",
    router: "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
    factory: "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
    weth: "0x4200000000000000000000000000000000000006",
    currency: "ETH",
    explorer: "https://sepolia.basescan.org",
    swapUrl: (token) => `https://app.uniswap.org/explore/tokens/base/${token}`,
  },
  "bsc-testnet": {
    name: "BNB Smart Chain Testnet (PancakeSwap V2)",
    router: "0xD99D1c33F9fC3444f8101754aBC46c52416550D1",
    factory: "0x6725F303b657a9451d8BA641347777cd84270440",
    weth: "0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd",
    currency: "tBNB",
    explorer: "https://testnet.bscscan.com",
    swapUrl: (token) => `https://pancakeswap.finance/swap?outputCurrency=${token}&chain=bscTestnet`,
  },
};

const ROUTER_ABI = [
  "function factory() external view returns (address)",
  "function WETH() external view returns (address)",
  "function addLiquidityETH(address token, uint amountTokenDesired, uint amountTokenMin, uint amountETHMin, address to, uint deadline) external payable returns (uint amountToken, uint amountETH, uint liquidity)",
];

const FACTORY_ABI = [
  "function getPair(address tokenA, address tokenB) external view returns (address pair)",
];

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
  console.log(" 🌊 ComputeCoin (COMPUTE) — Liquidity Pool Setup");
  console.log("========================================================\n");

  let networkName = hre.globalOptions?.network;
  if (!networkName) {
    const netIdx = process.argv.indexOf("--network");
    if (netIdx !== -1 && process.argv[netIdx + 1]) {
      networkName = process.argv[netIdx + 1];
    }
  }
  networkName = networkName || process.env.HARDHAT_NETWORK || "sepolia";

  const dex = DEX_CONFIG[networkName] || {
    name: networkName,
    router: process.env.ROUTER_ADDRESS,
    factory: "",
    weth: "",
    currency: "ETH",
    explorer: "",
    swapUrl: (t) => "",
  };

  const routerAddress = process.env.ROUTER_ADDRESS || dex.router;
  if (!routerAddress) {
    console.error(`\n❌ ERROR: No DEX router defined for network '${networkName}'.`);
    console.error(`Set ROUTER_ADDRESS in contracts/.env to proceed.`);
    process.exit(1);
  }

  console.log(`📡 Network:    ${dex.name}`);
  console.log(`🔄 DEX Router: ${routerAddress}`);

  // 1. Resolve token address from deployment receipt or env
  const deploymentsDir = path.join(__dirname, "..", "deployments");
  const receiptPath = path.join(deploymentsDir, `${networkName}-token.json`);

  let tokenAddress = process.env.TOKEN_ADDRESS || process.env.COMPUTE_TOKEN_ADDRESS;
  if (!tokenAddress && fs.existsSync(receiptPath)) {
    try {
      const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf-8"));
      tokenAddress = receipt.tokenAddress;
      console.log(`📄 Found previous token deployment: ${tokenAddress}`);
    } catch (e) {
      // ignore
    }
  }

  if (!tokenAddress) {
    console.error(`\n❌ ERROR: ComputeCoin token address not found!`);
    console.error(`Please deploy the token first with:`);
    console.error(`  npm run deploy:token -- --network ${networkName}`);
    console.error(`or specify TOKEN_ADDRESS=0x... in contracts/.env\n`);
    process.exit(1);
  }

  // 2. Setup wallet & provider
  let privateKey = process.env.DEPLOYER_PRIVATE_KEY || process.env.PRIVATE_KEY;
  let rpcUrl = await resolveRpcUrl(networkName);

  if (!privateKey && (networkName === "localhost" || networkName === "hardhat")) {
    privateKey = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
  }

  if (!privateKey) {
    console.error("\n❌ ERROR: DEPLOYER_PRIVATE_KEY missing in contracts/.env");
    process.exit(1);
  }

  if (!privateKey.startsWith("0x")) {
    privateKey = "0x" + privateKey;
  }

  const provider = new ethers.JsonRpcProvider(rpcUrl);
  const wallet = new ethers.Wallet(privateKey, provider);
  console.log(`👛 Deployer:   ${wallet.address}`);

  // 3. Load token contract
  const artifact = await hre.artifacts.readArtifact("ComputeCoinToken");
  const tokenContract = new ethers.Contract(tokenAddress, artifact.abi, wallet);

  // 4. Check balances
  const tokenDecimals = await tokenContract.decimals();
  const tokenBalance = await tokenContract.balanceOf(wallet.address);
  const nativeBalance = await provider.getBalance(wallet.address);

  // Pool inputs
  const computeAmountTokens = process.env.POOL_COMPUTE_AMOUNT || "10000000"; // 10 million COMPUTE
  const nativeAmountTokens = process.env.POOL_NATIVE_AMOUNT || "0.005"; // 0.005 ETH

  const computeAmountMinor = ethers.parseUnits(computeAmountTokens, tokenDecimals);
  const nativeAmountMinor = ethers.parseEther(nativeAmountTokens);

  console.log(`\n📊 Planned Liquidity:`);
  console.log(`   Tokens: ${Number(computeAmountTokens).toLocaleString()} COMPUTE`);
  console.log(`   Native: ${nativeAmountTokens} ${dex.currency}`);

  if (tokenBalance < computeAmountMinor) {
    console.error(`\n❌ ERROR: Insufficient COMPUTE token balance!`);
    console.error(`   Required: ${computeAmountTokens} COMPUTE`);
    console.error(`   Actual:   ${ethers.formatUnits(tokenBalance, tokenDecimals)} COMPUTE`);
    process.exit(1);
  }

  if (nativeBalance < nativeAmountMinor) {
    console.error(`\n❌ ERROR: Insufficient native gas balance!`);
    console.error(`   Required: ${nativeAmountTokens} ${dex.currency} + gas`);
    console.error(`   Actual:   ${ethers.formatEther(nativeBalance)} ${dex.currency}`);
    process.exit(1);
  }

  // 5. Connect router
  const router = new ethers.Contract(routerAddress, ROUTER_ABI, wallet);

  // 6. Check & approve ERC-20 allowance
  console.log("\n🔑 Step 1/2: Checking Router allowance for COMPUTE...");
  const currentAllowance = await tokenContract.allowance(wallet.address, routerAddress);
  if (currentAllowance < computeAmountMinor) {
    console.log("   Approving Router to transfer COMPUTE tokens...");
    const approveTx = await tokenContract.approve(routerAddress, ethers.MaxUint256);
    console.log(`   ⏳ Approval tx: ${approveTx.hash}`);
    await approveTx.wait(1);
    console.log("   ✅ Allowance approved successfully!");
  } else {
    console.log("   ✅ Allowance already sufficient.");
  }

  // 7. Add liquidity
  console.log("\n🏊 Step 2/2: Adding Liquidity to Router (addLiquidityETH)...");
  const deadline = Math.floor(Date.now() / 1000) + 60 * 20; // 20 minutes from now

  const addTx = await router.addLiquidityETH(
    tokenAddress,
    computeAmountMinor,
    0, // slippage tolerance: 0 for initial pool creation
    0,
    wallet.address,
    deadline,
    { value: nativeAmountMinor }
  );

  console.log(`   ⏳ AddLiquidity tx: ${addTx.hash}`);
  const receipt = await addTx.wait(1);
  console.log("   ✅ Liquidity successfully added!");

  // 8. Discover LP Pair Address
  let pairAddress = "N/A";
  try {
    const factoryAddress = dex.factory || (await router.factory());
    const wethAddress = dex.weth || (await router.WETH());
    const factory = new ethers.Contract(factoryAddress, FACTORY_ABI, provider);
    pairAddress = await factory.getPair(tokenAddress, wethAddress);
  } catch (err) {
    console.log("   (Could not automatically query pair address: " + err.message + ")");
  }

  // Calculate implied price
  const impliedPriceTokensPerEth = Number(computeAmountTokens) / Number(nativeAmountTokens);
  const impliedPriceEthPerToken = Number(nativeAmountTokens) / Number(computeAmountTokens);

  console.log("\n========================================================");
  console.log(" 🎉 LIQUIDITY POOL SUCCESSFULLY CREATED!");
  console.log("========================================================");
  console.log(` COMPUTE Token:   ${tokenAddress}`);
  console.log(` Router Address:  ${routerAddress}`);
  console.log(` LP Pair Address: ${pairAddress}`);
  console.log(` Initial Liquidity: ${Number(computeAmountTokens).toLocaleString()} COMPUTE + ${nativeAmountTokens} ${dex.currency}`);
  console.log(` Implied Initial Rate: 1 ${dex.currency} = ${impliedPriceTokensPerEth.toLocaleString()} COMPUTE`);
  console.log(`                      1 COMPUTE = ${impliedPriceEthPerToken.toFixed(10)} ${dex.currency}`);

  const swapLink = dex.swapUrl ? dex.swapUrl(tokenAddress) : "";
  if (swapLink) {
    console.log(`\n 💱 Testnet DEX Swap Page:`);
    console.log(`    👉 ${swapLink}`);
  }

  if (dex.explorer && pairAddress !== "N/A") {
    console.log(` 🔍 LP Pair on Block Explorer:`);
    console.log(`    👉 ${dex.explorer}/address/${pairAddress}`);
  }
  console.log("========================================================\n");

  // Save pool record
  const poolReceipt = {
    network: networkName,
    tokenAddress,
    routerAddress,
    pairAddress,
    computeAmount: computeAmountTokens,
    nativeAmount: nativeAmountTokens,
    currency: dex.currency,
    impliedPriceTokensPerNative: impliedPriceTokensPerEth,
    transactionHash: addTx.hash,
    swapUrl: swapLink,
    createdAt: new Date().toISOString(),
  };

  const poolReceiptPath = path.join(deploymentsDir, `${networkName}-liquidity-pool.json`);
  fs.writeFileSync(poolReceiptPath, JSON.stringify(poolReceipt, null, 2));
  console.log(`💾 Saved pool record to: ${path.relative(process.cwd(), poolReceiptPath)}\n`);
}

main().catch((error) => {
  console.error("\n❌ Liquidity pool creation failed:", error);
  process.exit(1);
});
