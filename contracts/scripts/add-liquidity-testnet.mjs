/**
 * ComputeCoin — add testnet liquidity (makes the coin "tradable" on a DEX).
 *
 * Pairs your COMPUTE token with the native testnet coin (tBNB / testnet ETH) in a
 * Uniswap-v2-style router pool (PancakeSwap on BSC Testnet by default). After this,
 * anyone can swap against the pool on the DEX — with TEST money only.
 *
 * ENV:
 *   RPC_URL                 testnet JSON-RPC
 *   PRIVATE_KEY             your testnet wallet (holds the COMPUTE supply)
 *   TOKEN_ADDRESS           the deployed ComputeCoinToken address
 *   ROUTER_ADDRESS          DEX router (default: PancakeSwap v2 BSC Testnet)
 *   TOKEN_AMOUNT            how many COMPUTE to add (human units, e.g. "100000")
 *   NATIVE_AMOUNT           how much tBNB/testETH to pair (e.g. "0.1")
 *   EXPLORER_BASE           (optional) for a tx link
 *
 * Run:
 *   TOKEN_ADDRESS=0x... TOKEN_AMOUNT=100000 NATIVE_AMOUNT=0.1 node scripts/add-liquidity-testnet.mjs
 *
 * NOTE: liquidity you add is the entire tradable depth. On testnet this is free
 * practice money. On mainnet (later, not here) a tiny pool gets drained by bots —
 * that's why real launch needs real liquidity + an audit first.
 */
import "dotenv/config";
import { ethers } from "ethers";

// PancakeSwap v2 router on BSC Testnet (chainId 97). Override via ROUTER_ADDRESS
// for other chains (e.g. a Uniswap v2 router on your testnet).
const DEFAULT_ROUTER = "0xD99D1c33F9fC3444f8101754aBC46c52416550D1";

const ROUTER_ABI = [
  "function factory() view returns (address)",
  "function WETH() view returns (address)",
  "function addLiquidityETH(address token,uint amountTokenDesired,uint amountTokenMin,uint amountETHMin,address to,uint deadline) payable returns (uint amountToken,uint amountETH,uint liquidity)",
];
const ERC20_ABI = [
  "function approve(address spender,uint256 amount) returns (bool)",
  "function decimals() view returns (uint8)",
  "function balanceOf(address) view returns (uint256)",
  "function allowance(address,address) view returns (uint256)",
];

function need(k) {
  const v = process.env[k];
  if (!v) throw new Error(`Missing env ${k}`);
  return v;
}

async function main() {
  const provider = new ethers.JsonRpcProvider(need("RPC_URL"));
  const net = await provider.getNetwork();
  const MAINNET = new Set([1n, 56n, 137n, 8453n, 42161n, 10n]);
  if (MAINNET.has(net.chainId) && process.env.I_UNDERSTAND_MAINNET !== "yes") {
    throw new Error(`Refusing to add liquidity on mainnet chainId ${net.chainId}. Testnet only.`);
  }

  const wallet = new ethers.Wallet(need("PRIVATE_KEY"), provider);
  const me = await wallet.getAddress();
  const tokenAddr = need("TOKEN_ADDRESS");
  const routerAddr = process.env.ROUTER_ADDRESS || DEFAULT_ROUTER;
  const tokenAmtHuman = need("TOKEN_AMOUNT");
  const nativeAmtHuman = need("NATIVE_AMOUNT");
  const explorer = process.env.EXPLORER_BASE?.replace(/\/$/, "") || "";

  const token = new ethers.Contract(tokenAddr, ERC20_ABI, wallet);
  const router = new ethers.Contract(routerAddr, ROUTER_ABI, wallet);

  const decimals = await token.decimals();
  const tokenAmt = ethers.parseUnits(tokenAmtHuman, decimals);
  const nativeAmt = ethers.parseEther(nativeAmtHuman);

  const tokenBal = await token.balanceOf(me);
  const nativeBal = await provider.getBalance(me);
  console.log(`Chain ${net.chainId} · wallet ${me}`);
  console.log(`COMPUTE balance: ${ethers.formatUnits(tokenBal, decimals)} · native: ${ethers.formatEther(nativeBal)}`);
  if (tokenBal < tokenAmt) throw new Error("Not enough COMPUTE in wallet for TOKEN_AMOUNT.");
  if (nativeBal <= nativeAmt) throw new Error("Not enough native testnet coin for NATIVE_AMOUNT + gas.");

  // 1) Approve the router to pull the tokens.
  console.log(`Approving router ${routerAddr} to spend ${tokenAmtHuman} COMPUTE…`);
  const approveTx = await token.approve(routerAddr, tokenAmt);
  await approveTx.wait();
  console.log(`  approved (${approveTx.hash})`);

  // 2) Add liquidity (COMPUTE + native). This creates the pair if it doesn't exist.
  const deadline = Math.floor(Date.now() / 1000) + 1200; // 20 min
  const minToken = (tokenAmt * 95n) / 100n; // 5% slippage tolerance
  const minNative = (nativeAmt * 95n) / 100n;
  console.log(`Adding liquidity: ${tokenAmtHuman} COMPUTE + ${nativeAmtHuman} native…`);
  const tx = await router.addLiquidityETH(
    tokenAddr, tokenAmt, minToken, minNative, me, deadline, { value: nativeAmt }
  );
  const rcpt = await tx.wait();
  console.log(`\n✅ Liquidity added. Your coin is now tradable on the DEX (testnet).`);
  console.log(`  tx: ${tx.hash}`);
  if (explorer) console.log(`  ${explorer}/tx/${tx.hash}`);
  console.log(`  Pool pairs COMPUTE (${tokenAddr}) with the native testnet coin.`);
}

main().catch((e) => {
  console.error("\n❌ add-liquidity failed:", e.message);
  process.exit(1);
});
