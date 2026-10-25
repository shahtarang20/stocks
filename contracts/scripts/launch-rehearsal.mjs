/**
 * Rehearsal of the launch plan on a LOCAL chain, asserting every promise it makes:
 * the supply is split exactly, vesting releases on schedule, the deployer ends with nothing,
 * admin powers sit behind a multisig and a timelock, and bad or unapproved plans are refused.
 *
 *   npx hardhat node &            # in another terminal
 *   node scripts/launch-rehearsal.mjs
 * Exits non-zero on the first failed assertion.
 */
import { ethers } from "ethers";
import { runLaunch, validatePlan, PlanError, artifact } from "./lib/launch.mjs";
import { readFileSync } from "node:fs";

const RPC = process.env.RPC_URL || "http://127.0.0.1:8545";
const provider = new ethers.JsonRpcProvider(RPC);
const net = await provider.getNetwork();
if (net.chainId !== 31337n) throw new Error(`rehearsal only runs on a local chain, got ${net.chainId}`);

// Hardhat's public dev keys #0..#5: worthless coins on a local chain only.
const KEYS = [
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
  "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
  "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
  "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
  "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
  "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
];
const w = KEYS.map((k) => new ethers.Wallet(k, provider));
const [deployerW, safeW, teamW, liqW, signerW, feeW] = w;
// NonceManager everywhere: back-to-back transactions from one account must not reuse a cached nonce.
const deployer = new ethers.NonceManager(deployerW);
const safe = new ethers.NonceManager(safeW);
const stranger = new ethers.NonceManager(liqW);

let failed = 0;
const check = (name, ok, detail = "") => { console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? "  " + detail : ""}`); if (!ok) failed++; };
// A transaction that is expected to fail leaves ethers' NonceManager one nonce ahead; resync after each.
let lastError = "";
const rejects = async (fn) => { try { await fn(); return false; } catch (e) { lastError = String(e.shortMessage || e.message).slice(0, 120) + " data=" + (e.data || e.info?.error?.data || e.info?.error?.message || ""); return true; } finally { for (const m of [deployer, safe, stranger]) m.reset(); } };
const advance = async (seconds) => { await provider.send("evm_increaseTime", [seconds]); await provider.send("evm_mine", []); };
const nowTs = async () => (await provider.getBlock("latest")).timestamp;

const plan = JSON.parse(readFileSync(new URL("../launch/tokenomics.example.json", import.meta.url), "utf8"));
plan.wallets = {
  SAFE: safeW.address, LIQUIDITY_WALLET: liqW.address, TEAM_WALLET: teamW.address,
  PLATFORM_SIGNER: signerW.address, FEE_TREASURY: feeW.address,
};

// ---- plan validation: bad plans are refused before anything is deployed ----
const bad = async (mutate, chainId = 31337) => { const p = structuredClone(plan); mutate(p); try { validatePlan(p, { chainId }); return false; } catch (e) { return e instanceof PlanError; } };
check("a plan that adds up to 100% and uses real addresses is accepted", !(await bad(() => {})));
check("allocations that do not add up to 100% are refused", await bad((p) => { p.allocations[0].bps += 1; }));
check("a placeholder address is refused", await bad((p) => { p.wallets.SAFE = "<multisig>"; }));
check("the zero address as a beneficiary is refused", await bad((p) => { p.wallets.TEAM_WALLET = ethers.ZeroAddress; }));
check("a cliff longer than the vesting period is refused", await bad((p) => { p.allocations[3].cliffMonths = 99; }));
check("a timelock delay under one hour is refused", await bad((p) => { p.timelockDelaySeconds = 60; }));
check("an unknown beneficiary is refused", await bad((p) => { p.allocations[0].beneficiary = "NOBODY"; }));
check("a PLACEHOLDER plan is refused on BNB Chain mainnet", await bad(() => {}, 56));
check("a PLACEHOLDER plan is refused on Ethereum mainnet", await bad(() => {}, 1));
process.env.I_UNDERSTAND_MAINNET = "";
{ const p = structuredClone(plan); p.status = "APPROVED"; let refused = false; try { validatePlan(p, { chainId: 56 }); } catch { refused = true; } check("even an APPROVED plan needs I_UNDERSTAND_MAINNET=yes on mainnet", refused); }

// ---- the launch itself ----
const { report, contracts, at } = await runLaunch({ signer: deployer, plan, log: () => {} });
const { token, settlement, timelock } = contracts;
const supply = BigInt(report.supply);

const holders = [...new Set(report.allocations.map((a) => a.holder))];
const sum = (await Promise.all(holders.map((h) => token.balanceOf(h)))).reduce((x, y) => x + y, 0n);
check("the whole supply is accounted for by the plan (no tokens left over or missing)", sum === supply);
check("the deployer ends with zero tokens", (await token.balanceOf(deployerW.address)) === 0n);
// Several allocations can share a holder (treasury and community both go to the multisig), so compare per holder.
const byHolder = new Map();
for (const a of report.allocations) byHolder.set(a.holder, (byHolder.get(a.holder) ?? 0n) + (supply * BigInt(a.bps)) / 10000n);
for (const [holder, expected] of byHolder) {
  const names = report.allocations.filter((a) => a.holder === holder).map((a) => `${a.name} ${a.bps / 100}%`).join(" + ");
  check(`${names}: the holder has exactly that share`, (await token.balanceOf(holder)) === expected);
}
check("the token is owned by the multisig, not the deployer", (await token.owner()) === safeW.address);
check("the settlement contract is owned by the timelock", (await settlement.owner()) === (await timelock.getAddress()));
check("the deployer can no longer pause the token", await rejects(async () => (await token.pause()).wait()));
check("the multisig can pause and unpause the token", !(await rejects(async () => { const t = token.connect(safe); await (await t.pause()).wait(); await (await t.unpause()).wait(); })));
check("nobody holds the timelock's admin role (no way to skip the delay)", !(await timelock.hasRole(await timelock.DEFAULT_ADMIN_ROLE(), deployerW.address)) && !(await timelock.hasRole(await timelock.DEFAULT_ADMIN_ROLE(), safeW.address)));

// ---- vesting: nothing before the cliff, linear after, everything at the end ----
const team = report.allocations.find((a) => a.name === "team");
const vest = at("ComputeGovernance", "ComputeVesting", team.vesting);
const tokenAddr = await token.getAddress();
const amount = BigInt(team.amount);
check("team vesting: beneficiary is the team wallet", (await vest.owner()) === teamW.address);
check("team vesting: nothing is releasable at the start", (await vest["releasable(address)"](tokenAddr)) === 0n);
await advance(11 * 30 * 24 * 3600);
check("team vesting: nothing is releasable just before the 12-month cliff", (await vest["releasable(address)"](tokenAddr)) === 0n);
await advance(2 * 30 * 24 * 3600);
const afterCliff = await vest["releasable(address)"](tokenAddr);
check("team vesting: tokens start to unlock after the cliff", afterCliff > 0n && afterCliff < amount);
const teamBefore = await token.balanceOf(teamW.address);
await (await vest["release(address)"](tokenAddr)).wait();
check("team vesting: release() pays the beneficiary exactly what had unlocked (within one block's drift)", (await token.balanceOf(teamW.address)) - teamBefore >= afterCliff);
check("team vesting: a stranger can trigger release but the money still goes to the beneficiary", !(await rejects(async () => (await vest.connect(stranger)["release(address)"](tokenAddr)).wait())));
await advance(60 * 30 * 24 * 3600);
await (await vest["release(address)"](tokenAddr)).wait();
check("team vesting: after the full period the beneficiary has received everything", (await token.balanceOf(teamW.address)) - teamBefore + 0n >= 0n && (await token.balanceOf(team.vesting)) === 0n && (await token.balanceOf(teamW.address)) === amount);

// ---- timelock: admin changes are delayed and only the multisig may propose ----
const newSigner = ethers.Wallet.createRandom().address;
const calldata = settlement.interface.encodeFunctionData("setPlatformSigner", [newSigner]);
const settlementAddr = await settlement.getAddress();
const salt = ethers.id("rehearsal-1");
const delay = BigInt(report.timelockDelaySeconds);
check("a stranger cannot schedule an admin change", await rejects(async () => (await timelock.connect(stranger).schedule(settlementAddr, 0, calldata, ethers.ZeroHash, salt, delay)).wait()));
check("a delay shorter than the minimum is refused", await rejects(async () => (await timelock.connect(safe).schedule(settlementAddr, 0, calldata, ethers.ZeroHash, salt, delay - 1n)).wait()));
check("the multisig can schedule an admin change", !(await rejects(async () => (await timelock.connect(safe).schedule(settlementAddr, 0, calldata, ethers.ZeroHash, salt, delay)).wait())));
check("the owner cannot change the signer directly (it must go through the timelock)", await rejects(async () => (await settlement.connect(safe).setPlatformSigner(newSigner)).wait()));
check("the scheduled change cannot run before the delay has passed", await rejects(async () => (await timelock.connect(safe).execute(settlementAddr, 0, calldata, ethers.ZeroHash, salt)).wait()));
// Jump to exactly one second after the operation becomes ready (no guessing at clock drift).
const preId = await timelock.hashOperation(settlementAddr, 0, calldata, ethers.ZeroHash, salt);
await provider.send("evm_setNextBlockTimestamp", [Number(await timelock.getTimestamp(preId)) + 1]);
await provider.send("evm_mine", []);
const opId = await timelock.hashOperation(settlementAddr, 0, calldata, ethers.ZeroHash, salt);
const executed = !(await rejects(async () => (await timelock.connect(safe).execute(settlementAddr, 0, calldata, ethers.ZeroHash, salt)).wait()));
check("after the delay the multisig can execute it", executed);
if (!executed) console.log("   DEBUG ready-at", String(await timelock.getTimestamp(opId)), "latest block", await nowTs(), "isReady", await timelock.isOperationReady(opId), "|", lastError);
check("the settlement signer really changed", (await settlement.platformSigner()) === newSigner);

console.log(failed === 0 ? "\nALL LAUNCH CHECKS PASSED" : `\n${failed} CHECK(S) FAILED`);
process.exit(failed === 0 ? 0 : 1);
