/**
 * Go / no-go check to run immediately before a launch. Sends nothing, spends nothing.
 *
 *   EXPECT_CHAIN_ID=97 DOTENV_CONFIG_PATH=testnet.env \
 *     node scripts/preflight.mjs launch/tokenomics.json --pool launch/pool.testnet.json
 *
 *   --pool <pool.json>      also check the PancakeSwap pool configuration
 *   --report <launch.json>  the launch report (after launch, before the pool): checks the live token and pair
 *   --audited <file>        the frozen audited bytecode hashes (default launch/audited.json; required on mainnet)
 *   --freeze <file>         record the current contract hashes and git commit into <file> (do this once the
 *                           audit is finished, then commit the file); later runs refuse any different code
 *
 * Reads RPC_URL, PRIVATE_KEY (deployer) and EXPECT_CHAIN_ID from the environment. EXPECT_CHAIN_ID is
 * mandatory so a wrong RPC_URL cannot silently point the launch at a different chain.
 * Exit code 0 only if there is no FAIL line. WARN lines are decisions a human must have made.
 */
import "dotenv/config";
import { ethers } from "ethers";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { artifact, validatePlan, PlanError, MAINNET_CHAIN_IDS } from "./lib/launch.mjs";
import { validatePoolConfig, verifyDex, inspectPair, isMainnet, PoolError } from "./lib/pool.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const FLAGS = ["--pool", "--report", "--freeze", "--audited"];
const flag = (n) => { const i = args.indexOf(n); return i === -1 ? undefined : args[i + 1]; };
const planPath = args.find((a, i) => !a.startsWith("--") && !FLAGS.includes(args[i - 1]));
const poolPath = flag("--pool");
const reportPath = flag("--report");
const freezePath = flag("--freeze");
const auditedPath = flag("--audited") ?? join(root, "launch", "audited.json");
if (!planPath && !freezePath) throw new Error("usage: node scripts/preflight.mjs <tokenomics.json> [--pool pool.json] [--report launch.json] [--audited f] [--freeze f]");

const lines = [];
let fails = 0, warns = 0;
const out = (level, name, detail = "") => { lines.push(level); if (level === "FAIL") fails++; if (level === "WARN") warns++; console.log(`[${level}] ${name}${detail ? "  " + detail : ""}`); };
const pass = (n, d) => out("PASS", n, d);
const warn = (n, d) => out("WARN", n, d);
const fail = (n, d) => out("FAIL", n, d);
const guard = async (name, fn) => { try { await fn(); } catch (e) { fail(name, String(e.shortMessage || e.message).slice(0, 300)); } };

const PUBLIC_RPC_HINTS = ["bsc-dataseed", "publicnode", "llamarpc", "ankr.com", "rpc.sepolia.org", "binance.org", "bnbchain.org", "blastapi", "nodereal.io/v1/public", "1rpc.io", "drpc.org"];
const PROBES = { PUSH0: "0x5f5ff3", MCOPY: "0x6020600060005e60006000f3", "TSTORE/TLOAD": "0x600160005d60005c5060006000f3" };
const CONTRACTS = [["ComputeCoinToken", "ComputeCoinToken"], ["ComputeSettlement", "ComputeSettlement"], ["ComputeGovernance", "ComputeTimelock"], ["ComputeGovernance", "ComputeVesting"]];

/** Rebuilds from source first, so the hashes are of the code in the working tree, not of an old build. */
const compile = () => execFileSync(join(root, "node_modules", ".bin", "hardhat"), ["compile"], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
const hashes = () => { compile(); return Object.fromEntries(CONTRACTS.map(([f, n]) => [n, ethers.keccak256(artifact(f, n).bytecode)])); };
const git = (...a) => { try { return execFileSync("git", a, { cwd: root, encoding: "utf8" }).trim(); } catch { return null; } };

if (freezePath) {
  const dirty = git("status", "--porcelain", "--", "src", "package-lock.json", "hardhat.config.js");
  if (dirty) throw new Error(`contracts have uncommitted changes; commit them first:\n${dirty}`);
  const rec = { frozenAt: new Date().toISOString(), commit: git("rev-parse", "HEAD"), bytecodeHashes: hashes(), note: "Record the audit firm, report and date here." };
  writeFileSync(freezePath, JSON.stringify(rec, null, 2) + "\n");
  console.log(`Recorded ${freezePath}: commit ${rec.commit}\nCommit this file. From now on preflight refuses any contract code that differs.`);
  process.exit(0);
}

const need = (k) => process.env[k] || (fail(`env ${k}`, "not set"), null);
const rpcUrl = need("RPC_URL"), pk = need("PRIVATE_KEY"), expect = need("EXPECT_CHAIN_ID");
if (!rpcUrl || !pk || !expect) { console.log("\nNO-GO: set the missing environment variables."); process.exit(1); }

const provider = new ethers.JsonRpcProvider(rpcUrl);
const deployer = new ethers.Wallet(pk).address;
let chainId = 0n, mainnet = false, planObj = null, W = {};
console.log(`\nPreflight for ${planPath}  deployer ${deployer}\n`);

// ---------------------------------------------------------------- chain
await guard("rpc", async () => {
  chainId = (await provider.getNetwork()).chainId;
  if (String(chainId) !== String(expect)) return fail("chain id", `RPC_URL is chain ${chainId} but EXPECT_CHAIN_ID is ${expect}`);
  pass("chain id", `${chainId}`);
  const age = Math.floor(Date.now() / 1000) - (await provider.getBlock("latest")).timestamp;
  age > 120 ? fail("rpc freshness", `latest block is ${age}s old; the node is stale or out of sync`) : pass("rpc freshness", `latest block ${age}s old`);
});
mainnet = MAINNET_CHAIN_IDS.has(chainId);
mainnet ? warn("network", "MAINNET: real money. Everything below is held to the strict rules") : pass("network", "test network");
if (mainnet && PUBLIC_RPC_HINTS.some((h) => rpcUrl.includes(h))) warn("rpc privacy", "RPC_URL looks like a public endpoint; use a private / MEV-protected RPC so liquidity transactions cannot be front-run");

await guard("evm", async () => {
  const bad = [];
  for (const [name, data] of Object.entries(PROBES)) { try { await provider.call({ data }); } catch { bad.push(name); } }
  bad.length ? fail("evm opcodes", `the chain does not run ${bad.join(", ")}, which the contracts are compiled for (cancun); recompile with an older evmVersion`) : pass("evm opcodes", "PUSH0, MCOPY and TSTORE/TLOAD execute");
});

// ---------------------------------------------------------------- plan
let plan;
try { plan = JSON.parse(readFileSync(planPath, "utf8")); } catch (e) { fail("plan file", e.message); }
if (plan) {
  try {
    planObj = validatePlan(plan, { chainId });
    W = planObj.wallets;
    pass("plan", `status ${plan.status}; allocations add up to 100%`);
  } catch (e) { fail("plan", e instanceof PlanError ? e.message : e.message); }
}
if (planObj) {
  const holders = new Set(planObj.allocations.map((a) => a.beneficiary));
  const clash = Object.entries(W).filter(([k, a]) => holders.has(k) && a.toLowerCase() === deployer.toLowerCase()).map(([k]) => k);
  clash.length
    ? fail("deployer is not a beneficiary", `${clash.join(", ")} equals the deployer; the launch aborts after deploying because the deployer must end with zero tokens`)
    : pass("deployer is not a beneficiary");
  const roles = ["SAFE", "PLATFORM_SIGNER", "FEE_TREASURY"].filter((k) => W[k].toLowerCase() === deployer.toLowerCase());
  roles.length ? (mainnet ? fail : warn)("deployer keeps no role", `${roles.join(", ")} is the deployer's own address; the deployer key should hold no ongoing power`) : pass("deployer keeps no role");
  await guard("wallets", async () => {
    const isContract = async (a) => (await provider.getCode(a)) !== "0x";
    const safeIsContract = await isContract(W.SAFE);
    if (safeIsContract) {
      const safe = new ethers.Contract(W.SAFE, ["function getThreshold() view returns (uint256)", "function getOwners() view returns (address[])"], provider);
      try {
        const [t, o] = [await safe.getThreshold(), await safe.getOwners()];
        t >= 2n || !mainnet ? pass("SAFE is a multisig", `${t}-of-${o.length}`) : fail("SAFE is a multisig", `threshold is ${t}; mainnet needs at least 2 signers to approve`);
      } catch { (mainnet ? fail : warn)("SAFE is a multisig", "the SAFE address is a contract but does not answer like a Safe (getThreshold/getOwners)"); }
    } else (mainnet ? fail : warn)("SAFE is a multisig", mainnet ? "SAFE is a plain wallet: one key would hold the token and the admin powers" : "SAFE is a plain wallet (acceptable on a test network only)");
    if (mainnet && !(await isContract(W.FEE_TREASURY))) warn("fee treasury", "FEE_TREASURY is a plain wallet; a multisig is safer");
    if (W.PLATFORM_SIGNER.toLowerCase() === W.SAFE.toLowerCase()) warn("platform signer", "PLATFORM_SIGNER equals SAFE; the API needs a key that can sign vouchers, so this cannot work unless the SAFE is an ERC-1271 signer");
  });
  const delay = plan.timelockDelaySeconds;
  delay < 86400 && mainnet ? fail("timelock delay", `${delay}s; mainnet needs at least 24h`) : delay < 172800 && mainnet ? warn("timelock delay", `${delay}s; 48h is the usual minimum for a traded token`) : pass("timelock delay", `${delay}s`);
  plan.tokenOwner === "renounce"
    ? warn("pause switch", "token ownership is renounced: nobody can ever pause trading, and nobody can stop a transfer bug either")
    : plan.tokenOwner === "timelock"
      ? warn("pause switch", `the token owner is the timelock: a pause is delayed ${delay}s, so it cannot be an emergency stop`)
      : warn("pause switch", "the multisig can pause all token transfers (and therefore trading): buyers and scanners will see this; confirm it is intended");
}

// ---------------------------------------------------------------- code
await guard("artifacts", async () => {
  const current = hashes();
  pass("compiled from source", Object.entries(current).map(([n, h]) => `${n} ${h.slice(0, 10)}`).join(", "));
  if (existsSync(auditedPath)) {
    const rec = JSON.parse(readFileSync(auditedPath, "utf8"));
    const diff = Object.keys(current).filter((n) => rec.bytecodeHashes?.[n] !== current[n]);
    diff.length ? fail("matches the audited code", `${diff.join(", ")} differ from ${auditedPath} (commit ${rec.commit?.slice(0, 10)}); deploy only audited code`) : pass("matches the audited code", `commit ${rec.commit?.slice(0, 10)}`);
  } else (mainnet ? fail : warn)("matches the audited code", `${auditedPath} not found; after the audit run: node scripts/preflight.mjs x --freeze launch/audited.json and commit it`);
});
const dirty = git("status", "--porcelain", "--", "src", "scripts", "launch", "hardhat.config.js", "package-lock.json");
dirty === null ? warn("git", "not a git checkout; cannot confirm what is being deployed") : dirty ? (mainnet ? fail : warn)("git is clean", `uncommitted changes:\n${dirty.split("\n").map((l) => "      " + l).join("\n")}`) : pass("git is clean", `commit ${git("rev-parse", "--short", "HEAD")}`);

// ---------------------------------------------------------------- money
if (planObj) await guard("deployer balance", async () => {
  const bal = await provider.getBalance(deployer);
  const fee = await provider.getFeeData();
  const gasPrice = fee.maxFeePerGas ?? fee.gasPrice ?? 0n;
  const P = ethers.Wallet.createRandom().address;
  const est = async (f, n, a) => provider.estimateGas({ from: deployer, data: ethers.concat([artifact(f, n).bytecode, new ethers.Interface(artifact(f, n).abi).encodeDeploy(a)]) });
  const vestings = planObj.allocations.filter((a) => a.kind === "vesting").length;
  let gas = 0n;
  gas += await est("ComputeCoinToken", "ComputeCoinToken", [deployer]);
  gas += await est("ComputeSettlement", "ComputeSettlement", [P, W.PLATFORM_SIGNER, W.FEE_TREASURY, deployer]);
  gas += await est("ComputeGovernance", "ComputeTimelock", [planObj.timelockDelaySeconds, [W.SAFE], [W.SAFE], ethers.ZeroAddress]);
  gas += BigInt(vestings) * (await est("ComputeGovernance", "ComputeVesting", [W.SAFE, 1n, 2n, 1n]));
  gas += BigInt(planObj.allocations.length + 2) * 70_000n; // transfers and the two ownership hand-overs
  const need = (gas * gasPrice * 3n) / 2n; // 50% headroom for price moves
  bal >= need ? pass("deployer balance", `${ethers.formatEther(bal)} >= ${ethers.formatEther(need)} needed (${gas} gas at ${ethers.formatUnits(gasPrice, "gwei")} gwei, +50%)`)
    : fail("deployer balance", `${ethers.formatEther(bal)} but about ${ethers.formatEther(need)} is needed (${gas} gas at ${ethers.formatUnits(gasPrice, "gwei")} gwei, +50%); fund ${deployer}`);
});
if (mainnet && existsSync(join(root, "testnet.env"))) {
  const testnetKey = /^(?:PRIVATE_KEY|DEPLOYER_PRIVATE_KEY)=(\S+)/m.exec(readFileSync(join(root, "testnet.env"), "utf8"))?.[1];
  testnetKey && new ethers.Wallet(testnetKey).address.toLowerCase() === deployer.toLowerCase()
    ? fail("deployer key", "this is the throwaway key from testnet.env; never use it on mainnet") : pass("deployer key", "is not the testnet key");
}

// ---------------------------------------------------------------- pool
if (poolPath) {
  let cfg = null;
  try { cfg = validatePoolConfig(JSON.parse(readFileSync(poolPath, "utf8"))); pass("pool config", `${cfg.dex.key}, ${ethers.formatUnits(cfg.tokenAmount, 18)} COMPUTE + ${ethers.formatEther(cfg.nativeAmount)} ${cfg.dex.nativeSymbol}`); }
  catch (e) { fail("pool config", e.message); }
  if (cfg) {
    const poolMainnet = isMainnet(cfg.dex, chainId);
    poolMainnet !== mainnet && fail("pool network", `the pool preset is ${cfg.dex.key} (${cfg.dex.mainnet ? "mainnet" : "testnet"}) but RPC_URL is ${mainnet ? "mainnet" : "a test network"}`);
    await guard("dex", async () => { await verifyDex(provider, cfg.dex); pass("dex addresses", `${cfg.dex.name}: router, factory and WBNB match the chain`); });
    if (cfg.lp.mode === "custom" && poolMainnet) warn("lp lock", "mode custom: make sure lp.recipient is a locker contract you have read");
    if (planObj) {
      const liq = planObj.allocations.find((a) => a.beneficiary === "LIQUIDITY_WALLET");
      const supply = ethers.parseUnits("1000000000", 18);
      const share = liq ? (supply * BigInt(liq.bps)) / 10000n : 0n;
      !liq ? fail("pool amount", "the plan has no allocation for LIQUIDITY_WALLET")
        : cfg.tokenAmount > share ? fail("pool amount", `pool needs ${ethers.formatUnits(cfg.tokenAmount, 18)} COMPUTE but the liquidity allocation is ${ethers.formatUnits(share, 18)}`)
        : pass("pool amount", `${ethers.formatUnits(cfg.tokenAmount, 18)} of the ${ethers.formatUnits(share, 18)} liquidity allocation`);
      if (cfg.lp.mode === "lock") (W[cfg.lp.beneficiary] || ethers.isAddress(cfg.lp.beneficiary)) ? pass("lp beneficiary", cfg.lp.beneficiary) : fail("lp beneficiary", `"${cfg.lp.beneficiary}" is not a wallet in the plan`);
      await guard("liquidity wallet", async () => {
        const b = await provider.getBalance(W.LIQUIDITY_WALLET);
        const need = cfg.nativeAmount + ethers.parseEther("0.01");
        b >= need ? pass("liquidity wallet balance", `${ethers.formatEther(b)} ${cfg.dex.nativeSymbol}`) : warn("liquidity wallet balance", `${ethers.formatEther(b)}; it needs ${ethers.formatEther(need)} ${cfg.dex.nativeSymbol} before the pool step`);
      });
    }
    if (reportPath) await guard("live token", async () => {
      const rep = JSON.parse(readFileSync(reportPath, "utf8"));
      const info = await inspectPair(provider, cfg.dex, rep.ComputeCoinToken);
      info.empty ? pass("pair is untouched", info.exists ? "pair exists but is empty" : "no pair yet") : fail("pair is untouched", `${info.pair} already holds funds; someone seeded it first`);
    });
  }
}

console.log(`\n${fails === 0 ? "GO" : "NO-GO"}: ${fails} failure(s), ${warns} warning(s) to acknowledge.`);
if (fails === 0 && warns) console.log("A WARN is not a block, but each one is a decision someone must be able to explain.");
process.exit(fails === 0 ? 0 : 1);
