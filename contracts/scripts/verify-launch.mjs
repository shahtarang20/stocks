/**
 * Source-verify the launched contracts on BscScan (Etherscan API V2: one key covers BSC mainnet and
 * testnet, https://etherscan.io/myapikey), from the files the launch wrote.
 *
 *   node scripts/verify-launch.mjs launch.97.json [--pool pool.97.json]            # write the files, check them, send nothing
 *   ETHERSCAN_API_KEY=... node scripts/verify-launch.mjs launch.97.json --submit   # also submit to the explorer
 *
 * For every contract this writes verification/<chainId>/<Name>.<address>/ with the exact compiler
 * "standard JSON input" and the ABI-encoded constructor arguments, so it can also be verified by hand
 * on the explorer's "Solidity (Standard-Json-Input)" page if the API is down.
 *
 * Before anything is submitted, the input is recompiled locally with the cached solc and the result
 * is compared byte-for-byte with what was deployed on chain. If they differ nothing is sent, because
 * the explorer would reject it too.
 */
import "dotenv/config";
import { ethers } from "ethers";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const reportPath = args.find((a, i) => !a.startsWith("--") && args[i - 1] !== "--pool");
const poolPath = args.includes("--pool") ? args[args.indexOf("--pool") + 1] : undefined;
const submit = args.includes("--submit");
if (!reportPath) throw new Error("usage: node scripts/verify-launch.mjs <launch.<chainId>.json> [--pool pool.<chainId>.json] [--submit]");

const report = JSON.parse(readFileSync(reportPath, "utf8"));
const pool = poolPath ? JSON.parse(readFileSync(poolPath, "utf8")) : null;
const EXPLORERS = { 56: "https://bscscan.com", 97: "https://testnet.bscscan.com", 31337: "http://localhost (local chain, self-check only)" };
const explorer = EXPLORERS[report.chainId];
if (!explorer) throw new Error(`no explorer is configured for chain ${report.chainId}`);
const provider = process.env.RPC_URL ? new ethers.JsonRpcProvider(process.env.RPC_URL) : null;

const art = (file, name) => JSON.parse(readFileSync(join(root, "artifacts", "src", `${file}.sol`, `${name}.json`), "utf8"));
const buildInfo = (id) => JSON.parse(readFileSync(join(root, "artifacts", "build-info", `${id}.json`), "utf8"));

/** Everything the explorer needs about one deployed contract. */
async function describe(file, name, address, ctorArgs) {
  const a = art(file, name);
  const bi = buildInfo(a.buildInfoId);
  return {
    name, address, file, artifact: a,
    contractName: `${a.inputSourceName}:${a.contractName}`,
    input: bi.input,
    compiler: `v${bi.solcLongVersion}`,
    ctor: new ethers.Interface(a.abi).encodeDeploy(ctorArgs).slice(2),
  };
}

const jobs = [];
const W = report.wallets;
jobs.push(await describe("ComputeCoinToken", "ComputeCoinToken", report.ComputeCoinToken, [report.deployer]));
jobs.push(await describe("ComputeSettlement", "ComputeSettlement", report.ComputeSettlement, [report.ComputeCoinToken, W.PLATFORM_SIGNER, W.FEE_TREASURY, report.deployer]));
jobs.push(await describe("ComputeGovernance", "ComputeTimelock", report.ComputeTimelock, [report.timelockDelaySeconds, [W.SAFE], [W.SAFE], ethers.ZeroAddress]));
const vestingAbi = art("ComputeGovernance", "ComputeVesting").abi;
for (const al of report.allocations.filter((x) => x.vesting)) {
  if (!provider) throw new Error("set RPC_URL: the vesting duration and cliff are read from each vesting contract");
  const v = new ethers.Contract(al.vesting, vestingAbi, provider);
  const [start, duration, cliff] = [await v.start(), await v.duration(), (await v.cliff()) - (await v.start())];
  jobs.push(await describe("ComputeGovernance", "ComputeVesting", al.vesting, [al.beneficiary, start, duration, cliff]));
}
if (pool?.lpLock) {
  if (!provider) throw new Error("set RPC_URL: the LP lock's timings are read from the contract");
  const v = new ethers.Contract(pool.lpLock, vestingAbi, provider);
  const [start, duration] = [await v.start(), await v.duration()];
  jobs.push(await describe("ComputeGovernance", "ComputeVesting", pool.lpLock, [pool.lpBeneficiary, start, duration, duration]));
}

/** Recompile the exact input and compare with the bytecode on chain (constructor args stripped). */
function findSolc(compiler) {
  const bin = join(homedir(), ".cache", "hardhat-nodejs", "compilers-v3", "linux-amd64", `solc-linux-amd64-${compiler}`);
  return existsSync(bin) ? bin : null;
}
async function selfCheck(job) {
  const solc = findSolc(job.compiler);
  if (!solc) return { ok: null, note: `solc ${job.compiler} not in the local cache; skipped the byte-for-byte check` };
  const res = spawnSync(solc, ["--standard-json"], { input: JSON.stringify(job.input), encoding: "utf8", maxBuffer: 256 * 1024 * 1024 });
  const out = JSON.parse(res.stdout);
  const compiled = out.contracts?.[job.artifact.inputSourceName]?.[job.artifact.contractName]?.evm?.bytecode?.object;
  if (!compiled) return { ok: false, note: `recompile failed: ${JSON.stringify(out.errors?.slice(0, 2))}` };
  if ("0x" + compiled !== job.artifact.bytecode) return { ok: false, note: "recompiled creation code differs from the build artifact" };
  if (provider) {
    const onchain = await provider.getCode(job.address);
    const runtime = "0x" + out.contracts[job.artifact.inputSourceName][job.artifact.contractName].evm.deployedBytecode.object;
    // Immutables are filled in at deploy time, so compare the length and the code up to the first difference only loosely.
    if (onchain === "0x") return { ok: false, note: "nothing is deployed at that address" };
    if (onchain.length !== runtime.length) return { ok: false, note: `on-chain code length ${onchain.length} differs from the build ${runtime.length}` };
  }
  return { ok: true, note: "recompiled input matches the build artifact" + (provider ? " and the deployed code length" : "") };
}

const dir = join(root, "verification", String(report.chainId));
mkdirSync(dir, { recursive: true });
let blocked = false;
for (const j of jobs) {
  const d = join(dir, `${j.name}.${j.address}`);
  mkdirSync(d, { recursive: true });
  writeFileSync(join(d, "standard-input.json"), JSON.stringify(j.input));
  writeFileSync(join(d, "constructor-args.hex"), j.ctor + "\n");
  writeFileSync(join(d, "README.txt"), `${j.name} at ${j.address}\nExplorer: ${explorer}/address/${j.address}#code\nCompiler: ${j.compiler}, optimizer 200 runs, EVM cancun\nContract name to select: ${j.contractName}\nUpload standard-input.json as "Solidity (Standard-Json-Input)"; constructor arguments (ABI-encoded) are in constructor-args.hex.\n`);
  j.check = await selfCheck(j);
  console.log(`${j.check.ok === false ? "[FAIL]" : j.check.ok ? "[PASS]" : "[SKIP]"} ${j.name.padEnd(18)} ${j.address}  ${j.check.note}`);
  if (j.check.ok === false) blocked = true;
}
console.log(`\nWrote ${dir}`);
if (!submit) { console.log("Nothing was submitted. Add --submit (with ETHERSCAN_API_KEY) to send them to the explorer."); process.exit(blocked ? 1 : 0); }
if (blocked) { console.error("Not submitting: at least one contract failed the local check."); process.exit(1); }

const key = process.env.ETHERSCAN_API_KEY;
if (!key) throw new Error("ETHERSCAN_API_KEY is not set");
const api = `https://api.etherscan.io/v2/api?chainid=${report.chainId}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let bad = 0;
for (const j of jobs) {
  const body = new URLSearchParams({
    apikey: key, module: "contract", action: "verifysourcecode", contractaddress: j.address,
    sourceCode: JSON.stringify(j.input), codeformat: "solidity-standard-json-input",
    contractname: j.contractName, compilerversion: j.compiler,
    constructorArguements: j.ctor, // sic: the API's own spelling
  });
  const first = await (await fetch(api, { method: "POST", body })).json();
  if (first.status !== "1") {
    if (/already verified/i.test(first.result ?? "")) { console.log(`[PASS] ${j.name}: already verified`); continue; }
    console.log(`[FAIL] ${j.name}: ${first.message} ${first.result}`); bad++; continue;
  }
  let state = "";
  for (let i = 0; i < 20; i++) {
    await sleep(5000);
    const s = await (await fetch(`${api}&module=contract&action=checkverifystatus&guid=${first.result}&apikey=${key}`)).json();
    state = s.result;
    if (!/pending|queue/i.test(state)) break;
  }
  const ok = /pass|already verified/i.test(state);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${j.name}: ${state}  ${explorer}/address/${j.address}#code`);
  if (!ok) bad++;
}
process.exit(bad ? 1 : 0);
