/**
 * Launch plan: deploy the token, settlement and governance contracts, split the fixed supply
 * according to a written allocation plan (some of it locked in vesting wallets), and hand control
 * of the contracts to a multisig / timelock so no single key keeps the supply or the admin powers.
 *
 * Pure library: scripts/launch.mjs runs it against a real network, scripts/launch-rehearsal.mjs
 * runs it against a local chain and asserts what it did.
 */
import { ethers } from "ethers";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

export const MAINNET_CHAIN_IDS = new Set([1n, 56n, 137n, 8453n, 42161n, 10n]);
const MONTH_SECONDS = 30n * 24n * 3600n;
const TOTAL_BPS = 10_000;

export function artifact(file, name) {
  const a = JSON.parse(readFileSync(join(root, "artifacts", "src", `${file}.sol`, `${name}.json`), "utf8"));
  if (!a.abi || !a.bytecode) throw new Error(`Artifact ${name} missing: run \`npx hardhat compile\` first`);
  return a;
}

export class PlanError extends Error {}

/** Throws PlanError describing the first problem. Returns the plan with resolved wallet addresses. */
export function validatePlan(plan, { chainId }) {
  const fail = (m) => { throw new PlanError(m); };
  if (!plan || typeof plan !== "object") fail("plan is not an object");
  if (MAINNET_CHAIN_IDS.has(BigInt(chainId))) {
    if (plan.status !== "APPROVED") fail(`plan status is "${plan.status}"; a mainnet launch needs status APPROVED, set by the owner after audit and legal review`);
    if (process.env.I_UNDERSTAND_MAINNET !== "yes") fail("mainnet launch also needs I_UNDERSTAND_MAINNET=yes");
  }
  const w = plan.wallets || {};
  const addr = (key) => {
    const v = w[key];
    if (typeof v !== "string" || !ethers.isAddress(v)) fail(`wallets.${key} is not a valid address (${v})`);
    if (v.toLowerCase() === ethers.ZeroAddress) fail(`wallets.${key} is the zero address`);
    return ethers.getAddress(v);
  };
  const wallets = Object.fromEntries(["SAFE", "PLATFORM_SIGNER", "FEE_TREASURY"].map((k) => [k, addr(k)]));
  for (const a of plan.allocations || []) if (typeof a.beneficiary === "string" && w[a.beneficiary]) wallets[a.beneficiary] = addr(a.beneficiary);

  if (!Number.isInteger(plan.timelockDelaySeconds) || plan.timelockDelaySeconds < 3600) fail("timelockDelaySeconds must be an integer of at least 3600 (1 hour)");
  if (!["safe", "timelock", "renounce"].includes(plan.tokenOwner)) fail('tokenOwner must be "safe", "timelock" or "renounce"');
  if (!["safe", "timelock"].includes(plan.settlementOwner)) fail('settlementOwner must be "safe" or "timelock"');
  if (!Array.isArray(plan.allocations) || plan.allocations.length === 0) fail("allocations is empty");

  let sum = 0;
  const names = new Set();
  for (const a of plan.allocations) {
    if (!a.name || names.has(a.name)) fail(`allocation name missing or duplicated (${a.name})`);
    names.add(a.name);
    if (!Number.isInteger(a.bps) || a.bps <= 0) fail(`${a.name}: bps must be a positive integer`);
    sum += a.bps;
    if (!wallets[a.beneficiary]) fail(`${a.name}: beneficiary "${a.beneficiary}" is not a known wallet`);
    if (a.kind === "vesting") {
      if (!Number.isInteger(a.vestingMonths) || a.vestingMonths <= 0) fail(`${a.name}: vestingMonths must be positive`);
      if (!Number.isInteger(a.cliffMonths) || a.cliffMonths < 0 || a.cliffMonths > a.vestingMonths) fail(`${a.name}: cliffMonths must be between 0 and vestingMonths`);
    } else if (a.kind !== "direct") fail(`${a.name}: kind must be "direct" or "vesting"`);
  }
  if (sum !== TOTAL_BPS) fail(`allocations add up to ${sum} bps; they must add up to exactly ${TOTAL_BPS} (100%)`);
  return { ...plan, wallets };
}

/**
 * @param signer   ethers Signer that deploys (it holds the supply only until the plan is executed)
 * @param plan     validated plan (see validatePlan)
 * @returns        everything that was deployed and the checks that were run
 */
export async function runLaunch({ signer, plan, log = () => {}, startTimestamp }) {
  const provider = signer.provider;
  const net = await provider.getNetwork();
  plan = validatePlan(plan, { chainId: net.chainId });
  const deployer = await signer.getAddress();
  const W = plan.wallets;

  const deploy = async (file, name, args) => {
    const art = artifact(file, name);
    const c = await new ethers.ContractFactory(art.abi, art.bytecode, signer).deploy(...args);
    await c.waitForDeployment();
    log(`  ${name}: ${await c.getAddress()}`);
    return c;
  };
  const at = (file, name, address) => new ethers.Contract(address, artifact(file, name).abi, signer);

  log("Deploying governance and token contracts…");
  // admin = address(0): the timelock administers itself, so nobody can skip its delay.
  const timelock = await deploy("ComputeGovernance", "ComputeTimelock", [plan.timelockDelaySeconds, [W.SAFE], [W.SAFE], ethers.ZeroAddress]);
  const token = await deploy("ComputeCoinToken", "ComputeCoinToken", [deployer]);
  const timelockAddr = await timelock.getAddress();
  const tokenAddr = await token.getAddress();
  const settlement = await deploy("ComputeSettlement", "ComputeSettlement", [tokenAddr, W.PLATFORM_SIGNER, W.FEE_TREASURY, deployer]);
  const settlementAddr = await settlement.getAddress();

  const supply = await token.totalSupply();
  const start = BigInt(startTimestamp ?? (await provider.getBlock("latest")).timestamp);
  const results = [];
  log("Distributing the supply…");
  for (const a of plan.allocations) {
    const amount = (supply * BigInt(a.bps)) / BigInt(TOTAL_BPS);
    const beneficiary = W[a.beneficiary];
    let holder = beneficiary;
    let vesting = null;
    if (a.kind === "vesting") {
      const v = await deploy("ComputeGovernance", "ComputeVesting", [beneficiary, start, BigInt(a.vestingMonths) * MONTH_SECONDS, BigInt(a.cliffMonths) * MONTH_SECONDS]);
      holder = vesting = await v.getAddress();
    }
    await (await token.transfer(holder, amount)).wait();
    results.push({ name: a.name, bps: a.bps, kind: a.kind, beneficiary, holder, vesting, amount: amount.toString() });
    log(`  ${a.name.padEnd(10)} ${String(a.bps / 100).padStart(5)}%  -> ${holder}${vesting ? "  (vesting)" : ""}`);
  }
  const left = await token.balanceOf(deployer);
  if (left !== 0n) throw new Error(`deployer still holds ${left} after distribution`);

  log("Handing over control…");
  const owner = { safe: W.SAFE, timelock: timelockAddr };
  if (plan.tokenOwner === "renounce") await (await token.renounceOwnership()).wait();
  else await (await token.transferOwnership(owner[plan.tokenOwner])).wait();
  await (await settlement.transferOwnership(owner[plan.settlementOwner])).wait();

  const report = {
    chainId: Number(net.chainId), deployer, status: plan.status,
    ComputeCoinToken: tokenAddr, ComputeSettlement: settlementAddr, ComputeTimelock: timelockAddr,
    timelockDelaySeconds: plan.timelockDelaySeconds, tokenOwner: plan.tokenOwner, settlementOwner: plan.settlementOwner,
    wallets: W, supply: supply.toString(), allocations: results, vestingStart: start.toString(),
    deployedAt: new Date().toISOString(),
  };
  return { report, contracts: { token, settlement, timelock }, at };
}
