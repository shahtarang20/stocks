/**
 * Rehearsal of the PancakeSwap pool launch on a LOCAL FORK of BNB Smart Chain mainnet, so the REAL
 * PancakeSwap router, factory and WBNB are used but no real funds are. Asserts every promise the pool
 * tooling makes: wrong or tampered inputs are refused, the pool is seeded at exactly the planned
 * price, the LP tokens cannot be pulled by anyone before the lock ends, and the token really trades.
 *
 *   node scripts/run-fork-rehearsal.mjs        # starts the fork, runs this, stops the fork
 * or, with a fork already running (hardhat node --fork <bsc rpc> --port 8599):
 *   RPC_URL=http://127.0.0.1:8599 node scripts/pool-fork-rehearsal.mjs
 * Exits non-zero on the first failed assertion.
 */
import { ethers } from "ethers";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { runLaunch, artifact } from "./lib/launch.mjs";
import {
  runPool, validatePoolConfig, resolveDex, inspectPair, lockStatus, planPool, PoolError,
  ROUTER_ABI, PAIR_ABI, ERC20_ABI, DEAD,
} from "./lib/pool.mjs";

const RPC = process.env.RPC_URL || "http://127.0.0.1:8599";
// cacheTimeout -1: ethers memoises identical reads for 250ms, which hands back a stale nonce after a fast transaction.
const provider = new ethers.JsonRpcProvider(RPC, undefined, { cacheTimeout: -1 });
const code = await provider.getCode("0x10ED43C718714eb63d5aA57B78B54704E256024E");
if (code === "0x") throw new Error("this is not a fork of BSC mainnet (no PancakeSwap router found); start it with --fork");

// Fresh random wallets. Not Hardhat's public dev keys: those addresses carry real mainnet history
// (sweeper contracts), which a fork would inherit.
const mk = async () => {
  const w = ethers.Wallet.createRandom().connect(provider);
  await provider.send("hardhat_setBalance", [w.address, "0x" + ethers.parseEther("100000").toString(16)]);
  return new ethers.NonceManager(w);
};
const [deployer, safe, liq, team, sig, fee, buyer, attacker] = await Promise.all(Array.from({ length: 8 }, mk));
const addr = async (s) => s.getAddress();
const managers = [deployer, safe, liq, team, sig, fee, buyer, attacker];

let failed = 0;
let lastRefusal = "";
const check = (name, ok, detail = "") => { console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? "  " + detail : ""}`); if (!ok) { failed++; console.log(`       last refusal seen: ${lastRefusal.slice(0, 300)}`); } };
const rejects = async (fn) => { try { await fn(); return false; } catch (e) { lastRefusal = String(e.shortMessage || e.message); return lastRefusal; } finally { managers.forEach((m) => m.reset()); } };
const advance = async (s) => { await provider.send("evm_increaseTime", [s]); await provider.send("evm_mine", []); };
const E = ethers.parseEther;

const dex = resolveDex("pancake-v2-bsc");
const quiet = () => {};
const baseEnv = {}; // deliberately empty: the mainnet confirmations are not set unless a check sets them

// ---- launch the contracts exactly as the real launch does ----
const plan = JSON.parse(readFileSync(new URL("../launch/tokenomics.example.json", import.meta.url), "utf8"));
plan.status = "REHEARSAL";
plan.wallets = {
  SAFE: await addr(safe), LIQUIDITY_WALLET: await addr(liq), TEAM_WALLET: await addr(team),
  PLATFORM_SIGNER: await addr(sig), FEE_TREASURY: await addr(fee),
};
const { report, contracts: { token } } = await runLaunch({ signer: deployer, plan, log: quiet });
const tokenAddr = report.ComputeCoinToken;
const liqAddr = await addr(liq);
const supply = await token.totalSupply();
check("launch: the liquidity wallet holds exactly the 10% liquidity share", (await token.balanceOf(liqAddr)) === supply / 10n);

const cfgRaw = {
  dex: "pancake-v2-bsc", tokenAmount: "100000000", nativeAmount: "5", slippageBps: 100,
  lp: { mode: "lock", lockMonths: 12, beneficiary: "SAFE" },
};
const cfg = validatePoolConfig(cfgRaw);
const run = (over = {}) => runPool({
  signer: liq, token: tokenAddr, config: cfg, wallets: report.wallets, log: quiet, allowChainMismatch: true,
  raw: { tokenAmount: cfgRaw.tokenAmount, nativeAmount: cfgRaw.nativeAmount }, env: baseEnv, ...over,
});
const tokenBefore = await token.balanceOf(liqAddr);
const nonceBefore = await provider.getTransactionCount(liqAddr);

// ---- the real PancakeSwap contracts are what the preset says they are ----
const router = new ethers.Contract(dex.router, ROUTER_ABI, provider);
check("dex: the preset's router, factory and WBNB match the chain", (await router.factory()) === dex.factory && (await router.WETH()) === dex.wnative);
const bad = resolveDex("pancake-v2-bsc");
bad.factory = ethers.getAddress("0x6725f303b657a9451d8ba641347777cd84270440"); // the wrong testnet factory this project once had
check("dex: a preset with a wrong factory address is refused", /router\.factory\(\) is|no contract at the factory/.test((await rejects(() => run({ config: { ...cfg, dex: bad } }))) || ""));

// ---- dry run changes nothing ----
const dry = await run();
check("dry run: reports the plan and sends nothing", dry.dryRun === true && (await provider.getTransactionCount(liqAddr)) === nonceBefore && (await token.balanceOf(liqAddr)) === tokenBefore);
check("dry run: price is 5 BNB for 100M COMPUTE", dry.summary.tokensPerNative === "20000000.0" && dry.summary.priceNativePerToken === "0.00000005");
check("dry run: states that the owner can pause trading", dry.summary.tokenOwnerCanPause === true);

// ---- mainnet confirmations ----
const refusedNoEnv = await rejects(() => run({ execute: true }));
check("mainnet: refused without I_UNDERSTAND_MAINNET and the typed confirmations", /I_UNDERSTAND_MAINNET/.test(refusedNoEnv || "") && (await provider.getTransactionCount(liqAddr)) === nonceBefore);
const refusedWrongAmount = await rejects(() => run({ execute: true, env: { I_UNDERSTAND_MAINNET: "yes", CONFIRM_TOKEN_AMOUNT: "100000000", CONFIRM_NATIVE_AMOUNT: "50" } }));
check("mainnet: refused when the native amount is mistyped (5 vs 50)", /CONFIRM_NATIVE_AMOUNT/.test(refusedWrongAmount || ""));
const eoaCfg = { ...cfg, lp: { mode: "custom", recipient: await addr(attacker) } };
check("mainnet: LP tokens may not go to a plain wallet", /plain wallet/.test((await rejects(() => run({ config: eoaCfg }))) || ""));
check("config: an over-large slippage, a bad amount and an unknown dex are all refused",
  [{ ...cfgRaw, slippageBps: 5000 }, { ...cfgRaw, tokenAmount: 100 }, { ...cfgRaw, nativeAmount: "0" }, { ...cfgRaw, dex: "uniswap" }, { ...cfgRaw, lp: { mode: "keep" } }]
    .every((c) => { try { validatePoolConfig(c); return false; } catch (e) { return e instanceof PoolError; } }));
const tooFew = await rejects(() => run({ signer: attacker }));
check("balance: a wallet that does not hold the COMPUTE is refused", /holds 0\.0 COMPUTE/.test(tooFew || ""));

// ---- someone seeds the pair before us: the price would be theirs ----
const factory = new ethers.Contract(dex.factory, ["function createPair(address,address) returns (address)", "function getPair(address,address) view returns (address)"], attacker);
await (await factory.createPair(tokenAddr, dex.wnative)).wait();
const pairAddr = await factory.getPair(tokenAddr, dex.wnative);
const wbnb = new ethers.Contract(dex.wnative, ["function deposit() payable", "function transfer(address,uint256) returns (bool)"], attacker);
await (await wbnb.deposit({ value: E("1") })).wait();
await (await wbnb.transfer(pairAddr, E("1"))).wait();
check("front-run: a pair that was created and funded in advance is detected", (await inspectPair(provider, dex, tokenAddr)).empty === false);
const refusedSeeded = await rejects(() => run({ execute: true, env: { I_UNDERSTAND_MAINNET: "yes", CONFIRM_TOKEN_AMOUNT: "100000000", CONFIRM_NATIVE_AMOUNT: "5" } }));
check("front-run: pool creation is refused and nothing is sent", /already holds funds/.test(refusedSeeded || "") && (await provider.getTransactionCount(liqAddr)) === nonceBefore);
await (await new ethers.Contract(pairAddr, ["function skim(address)"], attacker).skim(await addr(attacker))).wait();
check("front-run: after the donation is skimmed out the pair is clean again", (await inspectPair(provider, dex, tokenAddr)).empty === true);

// ---- the real thing, through the command-line tool an operator would use ----
const tmp = mkdtempSync(join(tmpdir(), "pool-rehearsal-"));
writeFileSync(join(tmp, "pool.json"), JSON.stringify(cfgRaw));
writeFileSync(join(tmp, "launch.json"), JSON.stringify({ ...report, status: "APPROVED" }));
writeFileSync(join(tmp, "launch-unapproved.json"), JSON.stringify(report));
const cli = (extra = [], env = {}, key = liq.signer.privateKey, reportFile = "launch.json") =>
  spawnSync(process.execPath, [fileURLToPath(new URL("./pool.mjs", import.meta.url)), join(tmp, "pool.json"), "--report", join(tmp, reportFile), ...extra], {
    cwd: tmp, encoding: "utf8", env: { PATH: process.env.PATH, RPC_URL: RPC, PRIVATE_KEY: key, POOL_ALLOW_FORK: "1", ...env },
  });
const confirm = { I_UNDERSTAND_MAINNET: "yes", CONFIRM_TOKEN_AMOUNT: "100000000", CONFIRM_NATIVE_AMOUNT: "5" };
const dryCli = cli();
check("cli: dry run exits 0, says nothing was sent, and sends nothing", dryCli.status === 0 && /DRY RUN/.test(dryCli.stdout) && (await provider.getTransactionCount(liqAddr)) === nonceBefore, dryCli.status === 0 ? "" : dryCli.stderr.slice(0, 200));
const unapproved = cli(["--execute"], confirm, liq.signer.privateKey, "launch-unapproved.json");
check("cli: a launch report that is not APPROVED is refused on mainnet", unapproved.status === 2 && /needs APPROVED/.test(unapproved.stderr));
const wrongKey = cli(["--execute"], confirm, deployer.signer.privateKey);
check("cli: a key that is not the plan's liquidity wallet is refused", wrongKey.status === 2 && /LIQUIDITY_WALLET/.test(wrongKey.stderr));
const noConfirm = cli(["--execute"]);
check("cli: --execute without the mainnet confirmations is refused", noConfirm.status === 2 && /CONFIRM_/.test(noConfirm.stderr) && (await provider.getTransactionCount(liqAddr)) === nonceBefore);
const tokenOnly = spawnSync(process.execPath, [fileURLToPath(new URL("./pool.mjs", import.meta.url)), join(tmp, "pool.json"), "--token", tokenAddr, "--execute"], {
  cwd: tmp, encoding: "utf8", env: { PATH: process.env.PATH, RPC_URL: RPC, PRIVATE_KEY: liq.signer.privateKey, POOL_ALLOW_FORK: "1", ...confirm },
});
check("cli: --token (no launch report) is refused on mainnet", tokenOnly.status === 2 && /launch report/.test(tokenOnly.stderr) && (await provider.getTransactionCount(liqAddr)) === nonceBefore);
const real = cli(["--execute"], confirm);
check("cli: --execute with every confirmation creates the pool and exits 0", real.status === 0 && /Pool created/.test(real.stdout), real.status === 0 ? "" : real.stderr.slice(0, 300) + real.stdout.slice(-300));
const summary = JSON.parse(readFileSync(join(tmp, `pool.${(await provider.getNetwork()).chainId}.json`), "utf8"));
const pair = new ethers.Contract(summary.pair, PAIR_ABI, provider);
const [r0, r1] = await pair.getReserves();
const tokenIs0 = (await pair.token0()).toLowerCase() === tokenAddr.toLowerCase();
const [rt, rn] = tokenIs0 ? [r0, r1] : [r1, r0];
check("pool: reserves are exactly the planned 100M COMPUTE and 5 BNB", rt === E("100000000") && rn === E("5"));
check("pool: the liquidity wallet spent exactly the pool amount of COMPUTE", tokenBefore - (await token.balanceOf(liqAddr)) === E("100000000"));
check("pool: no allowance is left on the router", (await token.allowance(liqAddr, dex.router)) === 0n);
const lpTotal = await pair.totalSupply();
check("pool: all LP tokens (bar the 1000 PancakeSwap burns itself) sit in the lock", (await pair.balanceOf(summary.lpLock)) === lpTotal - 1000n);
check("pool: neither the liquidity wallet nor the deployer holds any LP token", (await pair.balanceOf(liqAddr)) === 0n && (await pair.balanceOf(await addr(deployer))) === 0n);
const lockedFor = summary.lpUnlockAt - (await provider.getBlock("latest")).timestamp;
check("lock: 12 months (within a minute of 360 days)", Math.abs(lockedFor - 360 * 86400) < 120, `${lockedFor}s left`);

// ---- the lock cannot be opened early, by anyone ----
const lockInfo = await lockStatus(provider, summary.lpLock, summary.pair);
check("lock: the beneficiary is the multisig and nothing is releasable now", lockInfo.beneficiary === (await addr(safe)) && lockInfo.releasable === 0n);
const lockC = new ethers.Contract(summary.lpLock, artifact("ComputeGovernance", "ComputeVesting").abi, attacker);
await (await lockC["release(address)"](summary.pair)).wait();
check("lock: a stranger calling release() early moves nothing", (await pair.balanceOf(summary.lpLock)) === lpTotal - 1000n && (await pair.balanceOf(await addr(safe))) === 0n);
check("lock: nobody else can take the lock over", await rejects(async () => (await lockC.transferOwnership(await addr(attacker))).wait()));
await advance(11 * 30 * 86400);
check("lock: still shut after 11 months", (await lockStatus(provider, summary.lpLock, summary.pair)).releasable === 0n);

// ---- the token really trades ----
const buyerAddr = await addr(buyer);
const rBuyer = new ethers.Contract(dex.router, ROUTER_ABI, buyer);
const dl = (await provider.getBlock("latest")).timestamp + 600;
await (await rBuyer.swapExactETHForTokensSupportingFeeOnTransferTokens(0, [dex.wnative, tokenAddr], buyerAddr, dl, { value: E("0.1") })).wait();
const bought = await token.balanceOf(buyerAddr);
check("trading: a stranger can buy COMPUTE with BNB", bought > 0n);
const expectedOut = (E("0.1") * 9975n * rt) / (rn * 10000n + E("0.1") * 9975n); // PancakeSwap V2 charges 0.25%
check("trading: the amount received matches the constant-product price (0.25% fee)", bought === expectedOut, `${ethers.formatUnits(bought, 18)} COMPUTE`);
await (await token.connect(buyer).approve(dex.router, bought)).wait();
const bnbBefore = await provider.getBalance(buyerAddr);
await (await rBuyer.swapExactTokensForETHSupportingFeeOnTransferTokens(bought, 0, [tokenAddr, dex.wnative], buyerAddr, dl + 600)).wait();
check("trading: the buyer can sell it back for BNB", (await token.balanceOf(buyerAddr)) === 0n && (await provider.getBalance(buyerAddr)) > bnbBefore - E("0.01"));

// ---- the owner's pause switch is real, and visible ----
const safeAddr = await addr(safe);
check("token: the multisig owns the token", (await token.owner()) === safeAddr);
await (await token.connect(safe).pause()).wait();
check("token: while paused, trading reverts (this is the power buyers worry about)",
  await rejects(async () => (await rBuyer.swapExactETHForTokensSupportingFeeOnTransferTokens(0, [dex.wnative, tokenAddr], buyerAddr, dl + 900, { value: E("0.1") })).wait()));
await (await token.connect(safe).unpause()).wait();

// ---- the lock opens at the end, and only then ----
await advance(31 * 86400);
const end = await lockStatus(provider, summary.lpLock, summary.pair);
check("lock: after 12 months everything is releasable", end.releasable === end.held && end.held > 0n);
await (await lockC["release(address)"](summary.pair)).wait();
check("lock: release() hands all LP tokens to the multisig", (await pair.balanceOf(safeAddr)) === lpTotal - 1000n && (await pair.balanceOf(summary.lpLock)) === 0n);

// ---- burn mode, on a second token ----
const tokArt = artifact("ComputeCoinToken", "ComputeCoinToken");
const token2 = await new ethers.ContractFactory(tokArt.abi, tokArt.bytecode, liq).deploy(liqAddr);
await token2.waitForDeployment();
const burnCfg = validatePoolConfig({ ...cfgRaw, tokenAmount: "1000000", nativeAmount: "1", lp: { mode: "burn" } });
const burned = await runPool({ signer: liq, token: await token2.getAddress(), config: burnCfg, wallets: report.wallets, execute: true, log: quiet, allowChainMismatch: true, raw: { tokenAmount: "1000000", nativeAmount: "1" }, env: { I_UNDERSTAND_MAINNET: "yes", CONFIRM_TOKEN_AMOUNT: "1000000", CONFIRM_NATIVE_AMOUNT: "1" } });
const pair2 = new ethers.Contract(burned.summary.pair, PAIR_ABI, provider);
check("burn mode: every LP token is at the dead address and none anywhere else", (await pair2.balanceOf(DEAD)) === (await pair2.totalSupply()) - 1000n && (await pair2.balanceOf(liqAddr)) === 0n);

// ---- slippage bounds really stop a skewed pair (the sandwich) ----
const token3 = await new ethers.ContractFactory(tokArt.abi, tokArt.bytecode, attacker).deploy(await addr(attacker));
await token3.waitForDeployment();
const t3 = await token3.getAddress();
await (await token3.approve(dex.router, E("1000000"))).wait();
const rAtt = new ethers.Contract(dex.router, ROUTER_ABI, attacker);
const dl2 = (await provider.getBlock("latest")).timestamp + 600; // the clock moved a year: take a fresh deadline
// The attacker seeds the pair at a price 1000x off, in the gap before the project's transaction lands.
await (await rAtt.addLiquidityETH(t3, E("1000000"), 0, 0, await addr(attacker), dl2 + 100, { value: E("1") })).wait();
const bounds = planPool({ tokenAmount: E("100000"), nativeAmount: E("1"), slippageBps: 100, totalSupply: E("1000000000") });
await (await token3.transfer(liqAddr, E("100000"))).wait();
await (await token3.connect(liq).approve(dex.router, E("100000"))).wait();
const rLiq = new ethers.Contract(dex.router, ROUTER_ABI, liq);
check("slippage: adding liquidity into a pair skewed by an attacker reverts instead of losing funds",
  await rejects(async () => (await rLiq.addLiquidityETH(t3, E("100000"), bounds.minToken, bounds.minNative, liqAddr, dl2 + 200, { value: E("1") })).wait()));
check("slippage: with the old unbounded minimums the same add would have gone through at the attacker's price",
  !(await rejects(async () => (await rLiq.addLiquidityETH.staticCall(t3, E("100000"), 0, 0, liqAddr, dl2 + 200, { value: E("1") })))));

console.log(failed === 0 ? "\nALL POOL CHECKS PASSED" : `\n${failed} CHECK(S) FAILED`);
process.exit(failed === 0 ? 0 : 1);
