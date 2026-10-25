/**
 * Seed the first DEX liquidity pool for COMPUTE (PancakeSwap V2 on BNB Smart Chain).
 *
 * Pure library: scripts/pool.mjs runs it against a real network, scripts/pool-fork-rehearsal.mjs
 * runs it against a fork of BSC mainnet and asserts what it did.
 *
 * What it protects against, because each of these has cost real projects real money:
 *  - Wrong router/factory/WBNB address: they are checked against the chain, the factory and WBNB
 *    are read from the router itself and must match the preset.
 *  - A pair someone created and filled before you: the first liquidity fixes the price, so a
 *    non-empty pair is refused.
 *  - Sandwiching and price skew while the transaction is pending: amountMin bounds on both sides.
 *  - Unlimited approvals left on the router: the approval is exactly the amount, then verified zero.
 *  - Liquidity that the deployer can pull at any time (the "rug" every buyer checks for): LP tokens
 *    go to a burn address or a time lock, never to a plain wallet on mainnet.
 *  - Fat-fingered amounts: mainnet needs the amounts typed a second time as confirmation.
 */
import { ethers } from "ethers";
import { artifact, MAINNET_CHAIN_IDS } from "./launch.mjs";

export class PoolError extends Error {}

export const DEAD = "0x000000000000000000000000000000000000dEaD";
const MONTH_SECONDS = 30n * 24n * 3600n;
const BPS = 10_000n;
const MAX_SLIPPAGE_BPS = 500;
/** Native coin kept back for gas on top of the liquidity itself. */
const GAS_RESERVE = ethers.parseEther("0.01");

/**
 * Router, factory and wrapped-native addresses. `verifyDex` re-checks every one of them against the
 * chain on every run, so a wrong entry here fails loudly instead of sending funds to a stranger.
 */
export const DEX_PRESETS = {
  "pancake-v2-bsc": {
    name: "PancakeSwap V2 on BNB Smart Chain",
    chainId: 56,
    mainnet: true,
    router: "0x10ED43C718714eb63d5aA57B78B54704E256024E",
    factory: "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
    wnative: "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    nativeSymbol: "BNB",
    explorer: "https://bscscan.com",
    swapUrl: (token) => `https://pancakeswap.finance/swap?chain=bsc&outputCurrency=${token}`,
  },
  "pancake-v2-bsc-testnet": {
    name: "PancakeSwap V2 on BNB Smart Chain Testnet",
    chainId: 97,
    mainnet: false,
    router: "0xD99D1c33F9fC3444f8101754aBC46c52416550D1",
    factory: "0x6725F303b657a9451d8BA641348b6761A6CC7a17",
    wnative: "0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd",
    nativeSymbol: "tBNB",
    explorer: "https://testnet.bscscan.com",
    swapUrl: (token) => `https://pancakeswap.finance/swap?chain=bscTestnet&outputCurrency=${token}`,
  },
};

export const ROUTER_ABI = [
  "function factory() view returns (address)",
  "function WETH() view returns (address)",
  "function addLiquidityETH(address token,uint amountTokenDesired,uint amountTokenMin,uint amountETHMin,address to,uint deadline) payable returns (uint amountToken,uint amountETH,uint liquidity)",
  "function swapExactETHForTokensSupportingFeeOnTransferTokens(uint amountOutMin,address[] path,address to,uint deadline) payable",
  "function swapExactTokensForETHSupportingFeeOnTransferTokens(uint amountIn,uint amountOutMin,address[] path,address to,uint deadline)",
];
export const FACTORY_ABI = ["function getPair(address,address) view returns (address)"];
export const PAIR_ABI = [
  "function getReserves() view returns (uint112,uint112,uint32)",
  "function token0() view returns (address)",
  "function token1() view returns (address)",
  "function totalSupply() view returns (uint256)",
  "function balanceOf(address) view returns (uint256)",
  "function MINIMUM_LIQUIDITY() view returns (uint256)",
];
export const ERC20_ABI = [
  "function decimals() view returns (uint8)",
  "function symbol() view returns (string)",
  "function totalSupply() view returns (uint256)",
  "function balanceOf(address) view returns (uint256)",
  "function allowance(address,address) view returns (uint256)",
  "function approve(address,uint256) returns (bool)",
  "function owner() view returns (address)",
  "function paused() view returns (bool)",
];

export const isMainnet = (dex, chainId) => Boolean(dex.mainnet) || MAINNET_CHAIN_IDS.has(BigInt(chainId));

export function resolveDex(name) {
  const dex = DEX_PRESETS[name];
  if (!dex) throw new PoolError(`unknown dex "${name}"; use one of: ${Object.keys(DEX_PRESETS).join(", ")}`);
  return { key: name, ...dex };
}

/** Strict human-unit amount: a string like "100000" or "0.5", at most 18 decimals, greater than zero. */
export function parseAmount(label, value) {
  if (typeof value !== "string" || !/^\d+(\.\d{1,18})?$/.test(value)) {
    throw new PoolError(`${label} must be a decimal string such as "100000" or "0.5" (got ${JSON.stringify(value)})`);
  }
  const wei = ethers.parseUnits(value, 18);
  if (wei <= 0n) throw new PoolError(`${label} must be greater than zero`);
  return wei;
}

/** Validates a pool config (see launch/pool.example.json). Pure: no network. */
export function validatePoolConfig(cfg) {
  const fail = (m) => { throw new PoolError(m); };
  if (!cfg || typeof cfg !== "object") fail("pool config is not an object");
  const dex = resolveDex(cfg.dex);
  const tokenAmount = parseAmount("tokenAmount", cfg.tokenAmount);
  const nativeAmount = parseAmount("nativeAmount", cfg.nativeAmount);
  const slippageBps = cfg.slippageBps ?? 100;
  if (!Number.isInteger(slippageBps) || slippageBps < 1 || slippageBps > MAX_SLIPPAGE_BPS) {
    fail(`slippageBps must be an integer from 1 to ${MAX_SLIPPAGE_BPS} (got ${slippageBps})`);
  }
  const deadlineSeconds = cfg.deadlineSeconds ?? 600;
  if (!Number.isInteger(deadlineSeconds) || deadlineSeconds < 60 || deadlineSeconds > 3600) fail("deadlineSeconds must be between 60 and 3600");
  const lp = cfg.lp || {};
  if (!["burn", "lock", "custom"].includes(lp.mode)) fail('lp.mode must be "burn", "lock" or "custom"');
  if (lp.mode === "lock") {
    if (!Number.isInteger(lp.lockMonths) || lp.lockMonths < 1) fail("lp.lockMonths must be a positive integer");
    if (typeof lp.beneficiary !== "string" || lp.beneficiary === "") fail('lp.beneficiary is required for mode "lock" (a wallet name such as "SAFE", or an address)');
  }
  if (lp.mode === "custom" && !ethers.isAddress(lp.recipient || "")) fail('lp.recipient must be an address for mode "custom"');
  return { dex, tokenAmount, nativeAmount, slippageBps, deadlineSeconds, lp };
}

/** Price and bounds for the pool. Pure: no network. */
export function planPool({ tokenAmount, nativeAmount, slippageBps, totalSupply }) {
  const minToken = (tokenAmount * (BPS - BigInt(slippageBps))) / BPS;
  const minNative = (nativeAmount * (BPS - BigInt(slippageBps))) / BPS;
  const nativePerToken = (nativeAmount * 10n ** 18n) / tokenAmount; // 18-decimal fixed point
  const tokensPerNative = (tokenAmount * 10n ** 18n) / nativeAmount;
  const fdvNative = totalSupply === undefined ? undefined : (nativePerToken * totalSupply) / 10n ** 18n;
  return { minToken, minNative, nativePerToken, tokensPerNative, fdvNative };
}

/** Confirms the preset's addresses against the chain. Throws PoolError on any mismatch. */
export async function verifyDex(provider, dex, { allowChainMismatch = false } = {}) {
  const net = await provider.getNetwork();
  if (!allowChainMismatch && Number(net.chainId) !== dex.chainId) {
    throw new PoolError(`connected to chain ${net.chainId} but ${dex.key} is chain ${dex.chainId}`);
  }
  for (const [label, addr] of [["router", dex.router], ["factory", dex.factory], ["wrapped native", dex.wnative]]) {
    if ((await provider.getCode(addr)) === "0x") throw new PoolError(`no contract at the ${label} address ${addr}`);
  }
  const router = new ethers.Contract(dex.router, ROUTER_ABI, provider);
  const [factory, wnative] = [await router.factory(), await router.WETH()];
  if (factory.toLowerCase() !== dex.factory.toLowerCase()) throw new PoolError(`router.factory() is ${factory}, the preset says ${dex.factory}`);
  if (wnative.toLowerCase() !== dex.wnative.toLowerCase()) throw new PoolError(`router.WETH() is ${wnative}, the preset says ${dex.wnative}`);
  return { chainId: net.chainId };
}

/** What the pair for (token, wrapped native) looks like right now. */
export async function inspectPair(provider, dex, token) {
  const factory = new ethers.Contract(dex.factory, FACTORY_ABI, provider);
  const pair = await factory.getPair(token, dex.wnative);
  if (pair === ethers.ZeroAddress) return { exists: false, pair: null, empty: true };
  const pc = new ethers.Contract(pair, PAIR_ABI, provider);
  const tokenC = new ethers.Contract(token, ERC20_ABI, provider);
  const wn = new ethers.Contract(dex.wnative, ERC20_ABI, provider);
  const [r0, r1] = await pc.getReserves();
  const [lpSupply, tokenBal, wnBal] = [await pc.totalSupply(), await tokenC.balanceOf(pair), await wn.balanceOf(pair)];
  const empty = r0 === 0n && r1 === 0n && lpSupply === 0n && tokenBal === 0n && wnBal === 0n;
  return { exists: true, pair, empty, reserves: [r0, r1], lpSupply, tokenBal, wnBal };
}

export function assertPairUntouched(info) {
  if (info.exists && !info.empty) {
    throw new PoolError(
      `the pair ${info.pair} already holds funds (LP supply ${info.lpSupply}, token ${info.tokenBal}, wrapped native ${info.wnBal}). ` +
        "The first liquidity fixes the price, so someone else would set it. Do not continue; find out who seeded it.",
    );
  }
}

/** Mainnet needs the amounts typed a second time, so a stray zero is caught before it costs money. */
export function assertMainnetConfirmation({ tokenAmountText, nativeAmountText, env = process.env }) {
  const problems = [];
  if (env.I_UNDERSTAND_MAINNET !== "yes") problems.push("I_UNDERSTAND_MAINNET=yes is not set");
  if (env.CONFIRM_TOKEN_AMOUNT !== tokenAmountText) problems.push(`CONFIRM_TOKEN_AMOUNT must equal "${tokenAmountText}"`);
  if (env.CONFIRM_NATIVE_AMOUNT !== nativeAmountText) problems.push(`CONFIRM_NATIVE_AMOUNT must equal "${nativeAmountText}"`);
  if (problems.length) throw new PoolError(`mainnet pool creation refused: ${problems.join("; ")}`);
}

/**
 * Seeds the pool.
 * @param signer       ethers Signer that holds the COMPUTE for the pool and pays the native side
 * @param token        COMPUTE token address
 * @param config       output of validatePoolConfig
 * @param wallets      named wallets from the launch report (used to resolve lp.beneficiary)
 * @param execute      false = check and print only, send nothing
 * @param raw          the unparsed amounts as text (for the mainnet confirmation)
 */
export async function runPool({ signer, token, config, wallets = {}, execute = false, raw, log = () => {}, allowChainMismatch = false, env = process.env }) {
  const provider = signer.provider;
  const { dex, tokenAmount, nativeAmount, slippageBps, deadlineSeconds, lp } = config;
  const me = await signer.getAddress();
  const net = await provider.getNetwork();
  const mainnet = isMainnet(dex, net.chainId);

  log(`Checking ${dex.name}…`);
  await verifyDex(provider, dex, { allowChainMismatch });

  const tokenC = new ethers.Contract(token, ERC20_ABI, signer);
  if ((await provider.getCode(token)) === "0x") throw new PoolError(`no contract at token address ${token}`);
  const [decimals, symbol, supply] = [await tokenC.decimals(), await tokenC.symbol(), await tokenC.totalSupply()];
  if (Number(decimals) !== 18) throw new PoolError(`token has ${decimals} decimals; the amounts here assume 18`);
  if (tokenAmount > supply) throw new PoolError("tokenAmount is larger than the total supply");
  if (await tokenC.paused().catch(() => false)) throw new PoolError("the token is paused, so the router could not move it");
  const holding = await tokenC.balanceOf(me);
  if (holding < tokenAmount) {
    throw new PoolError(`${me} holds ${ethers.formatUnits(holding, 18)} ${symbol} but the pool needs ${ethers.formatUnits(tokenAmount, 18)}; use the liquidity wallet's key`);
  }
  const nativeBal = await provider.getBalance(me);
  if (nativeBal < nativeAmount + GAS_RESERVE) {
    throw new PoolError(`${me} holds ${ethers.formatEther(nativeBal)} ${dex.nativeSymbol}; needs ${ethers.formatEther(nativeAmount + GAS_RESERVE)} (liquidity plus gas)`);
  }
  const pairBefore = await inspectPair(provider, dex, token);
  assertPairUntouched(pairBefore);

  // Where the LP tokens go. A plain wallet means the holder can withdraw the whole pool at any time.
  let lpRecipient;
  let lockPlan = null;
  if (lp.mode === "burn") lpRecipient = DEAD;
  else if (lp.mode === "lock") {
    const beneficiary = wallets[lp.beneficiary] ?? lp.beneficiary;
    if (!ethers.isAddress(beneficiary) || beneficiary === ethers.ZeroAddress) throw new PoolError(`lp.beneficiary "${lp.beneficiary}" is not a known wallet or address`);
    lockPlan = { beneficiary: ethers.getAddress(beneficiary), seconds: BigInt(lp.lockMonths) * MONTH_SECONDS };
  } else {
    lpRecipient = ethers.getAddress(lp.recipient);
    if ((await provider.getCode(lpRecipient)) === "0x") {
      throw new PoolError(
        `lp.recipient ${lpRecipient} is a plain wallet, not a locker contract: its owner could withdraw the whole pool. ` +
          (mainnet ? "Refused on mainnet; use mode burn or lock." : "Allowed on a test network only."),
      );
    }
  }

  const plan = planPool({ tokenAmount, nativeAmount, slippageBps, totalSupply: supply });
  const tokenOwner = await tokenC.owner().catch(() => null);
  const summary = {
    dex: dex.key, chainId: Number(net.chainId), mainnet, token, symbol, signer: me,
    tokenAmount: tokenAmount.toString(), nativeAmount: nativeAmount.toString(),
    priceNativePerToken: ethers.formatUnits(plan.nativePerToken, 18),
    tokensPerNative: ethers.formatUnits(plan.tokensPerNative, 18),
    fullyDilutedValueNative: plan.fdvNative === undefined ? null : ethers.formatUnits(plan.fdvNative, 18),
    minToken: plan.minToken.toString(), minNative: plan.minNative.toString(), slippageBps,
    lpMode: lp.mode, lpLockMonths: lp.mode === "lock" ? lp.lockMonths : null, lpBeneficiary: lockPlan?.beneficiary ?? null,
    tokenOwner, tokenOwnerCanPause: tokenOwner !== null && tokenOwner !== ethers.ZeroAddress,
  };
  log(`  ${ethers.formatUnits(tokenAmount, 18)} ${symbol} + ${ethers.formatEther(nativeAmount)} ${dex.nativeSymbol}`);
  log(`  price ${summary.priceNativePerToken} ${dex.nativeSymbol} per ${symbol}; fully diluted value ${summary.fullyDilutedValueNative} ${dex.nativeSymbol}`);
  log(`  LP tokens: ${lp.mode}${lockPlan ? ` for ${lp.lockMonths} months, then to ${lockPlan.beneficiary}` : ""}`);
  log(`  token owner ${tokenOwner ?? "none"}${summary.tokenOwnerCanPause ? " (can pause all transfers, including trading)" : " (ownership renounced: no pause switch)"}`);

  if (!execute) return { dryRun: true, summary };
  if (mainnet) assertMainnetConfirmation({ tokenAmountText: raw?.tokenAmount, nativeAmountText: raw?.nativeAmount, env });

  const block = await provider.getBlock("latest");
  if (lockPlan) {
    log("Deploying the LP lock…");
    const art = artifact("ComputeGovernance", "ComputeVesting");
    // cliff == duration: nothing at all can be released until the end, then everything.
    const lock = await new ethers.ContractFactory(art.abi, art.bytecode, signer).deploy(lockPlan.beneficiary, BigInt(block.timestamp), lockPlan.seconds, lockPlan.seconds);
    await lock.waitForDeployment();
    lpRecipient = await lock.getAddress();
    summary.lpLock = lpRecipient;
    summary.lpUnlockAt = Number(block.timestamp) + Number(lockPlan.seconds);
    log(`  lock ${lpRecipient}, unlocks at ${new Date(summary.lpUnlockAt * 1000).toISOString()}`);
  }

  log("Approving exactly the pool amount…");
  await (await tokenC.approve(dex.router, tokenAmount)).wait();

  log("Adding liquidity…");
  const router = new ethers.Contract(dex.router, ROUTER_ABI, signer);
  const deadline = BigInt(block.timestamp) + BigInt(deadlineSeconds);
  const tx = await router.addLiquidityETH(token, tokenAmount, plan.minToken, plan.minNative, lpRecipient, deadline, { value: nativeAmount });
  const receipt = await tx.wait();
  summary.txHash = receipt.hash;

  // Leave nothing approved, then prove the result rather than trusting the transaction succeeded.
  if ((await tokenC.allowance(me, dex.router)) !== 0n) await (await tokenC.approve(dex.router, 0n)).wait();
  const after = await inspectPair(provider, dex, token);
  if (!after.exists) throw new PoolError("liquidity was added but the pair cannot be found");
  const pc = new ethers.Contract(after.pair, PAIR_ABI, provider);
  const token0 = await pc.token0();
  const [res0, res1] = after.reserves;
  const [tokenReserve, nativeReserve] = token0.toLowerCase() === token.toLowerCase() ? [res0, res1] : [res1, res0];
  const minLiquidity = await pc.MINIMUM_LIQUIDITY();
  const lpHeld = await pc.balanceOf(lpRecipient);
  const problems = [];
  if (tokenReserve < plan.minToken || tokenReserve > tokenAmount) problems.push(`token reserve ${tokenReserve} outside the planned bounds`);
  if (nativeReserve < plan.minNative || nativeReserve > nativeAmount) problems.push(`native reserve ${nativeReserve} outside the planned bounds`);
  if (lpHeld === 0n || lpHeld !== after.lpSupply - minLiquidity) problems.push(`LP recipient holds ${lpHeld} of ${after.lpSupply} (expected everything except the ${minLiquidity} burned at creation)`);
  if (problems.length) throw new PoolError(`pool created but failed verification: ${problems.join("; ")}`);

  Object.assign(summary, {
    pair: after.pair, lpRecipient, lpAmount: lpHeld.toString(),
    tokenReserve: tokenReserve.toString(), nativeReserve: nativeReserve.toString(),
    swapUrl: dex.swapUrl(token), explorerPair: `${dex.explorer}/address/${after.pair}`,
    createdAt: new Date().toISOString(),
  });
  return { dryRun: false, summary };
}

/** When can the locked LP tokens be released, and how many are releasable right now? */
export async function lockStatus(provider, lockAddress, pairAddress) {
  const art = artifact("ComputeGovernance", "ComputeVesting");
  const lock = new ethers.Contract(lockAddress, art.abi, provider);
  const [end, beneficiary, releasable, held] = [
    await lock.end(), await lock.owner(), await lock["releasable(address)"](pairAddress),
    await new ethers.Contract(pairAddress, PAIR_ABI, provider).balanceOf(lockAddress),
  ];
  return { unlocksAt: Number(end), beneficiary, releasable, held };
}
