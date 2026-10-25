/**
 * Dress rehearsal of the TESTNET launch: runs the exact files and commands of the real BSC-testnet launch
 * (launch/tokenomics.testnet.json, launch/pool.testnet.json, testnet.env, testnet-wallets.env) against a
 * local FORK of BSC testnet, with the fork's balances topped up instead of a faucet. If this passes, the
 * only thing the real launch still needs is test BNB in the deployer wallet.
 *
 *   node scripts/testnet-dress-rehearsal.mjs
 *
 * Needs contracts/testnet.env and contracts/testnet-wallets.env (git-ignored; see the launch checklist).
 */
import { ethers } from "ethers";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { startFork } from "./lib/fork.mjs";
import { ROUTER_ABI, DEX_PRESETS, lockStatus, PAIR_ABI } from "./lib/pool.mjs";
import { artifact } from "./lib/launch.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const parse = (f) => Object.fromEntries(readFileSync(f, "utf8").split("\n").filter((l) => /^[A-Z_]+=/.test(l)).map((l) => [l.split("=")[0], l.slice(l.indexOf("=") + 1).trim()]));
for (const f of ["testnet.env", "testnet-wallets.env"]) if (!existsSync(join(root, f))) throw new Error(`missing contracts/${f}`);
const T = parse(join(root, "testnet.env")), K = parse(join(root, "testnet-wallets.env"));
const plan = JSON.parse(readFileSync(join(root, "launch/tokenomics.testnet.json"), "utf8"));
const dex = DEX_PRESETS["pancake-v2-bsc-testnet"];

let failed = 0;
const check = (name, ok, detail = "") => { console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${ok || !detail ? "" : "  " + detail}`); if (!ok) failed++; };

const fork = await startFork({ target: "testnet", port: "8598" });
try {
  const provider = new ethers.JsonRpcProvider(fork.url, undefined, { cacheTimeout: -1 });
  const deployerKey = T.PRIVATE_KEY, liqKey = K.LIQUIDITY_WALLET_PRIVATE_KEY;
  const deployer = new ethers.Wallet(deployerKey).address, liqAddr = new ethers.Wallet(liqKey).address;
  check("the plan's liquidity wallet is the one whose key is on file", liqAddr.toLowerCase() === plan.wallets.LIQUIDITY_WALLET.toLowerCase());
  check("the real PancakeSwap testnet router answers with the preset's factory and WBNB",
    (await new ethers.Contract(dex.router, ROUTER_ABI, provider).factory()) === dex.factory && (await new ethers.Contract(dex.router, ROUTER_ABI, provider).WETH()) === dex.wnative);

  // Faucet stand-in: the same amounts the real wallets will need.
  for (const [a, v] of [[deployer, "0.2"], [liqAddr, "0.2"]]) await provider.send("hardhat_setBalance", [a, "0x" + ethers.parseEther(v).toString(16)]);

  const tmp = mkdtempSync(join(tmpdir(), "testnet-dress-"));
  // Async on purpose: the fork's caching proxy lives in THIS process, so blocking here would deadlock it.
  const run = (script, argv, key) => new Promise((resolve) => {
    const child = spawn(process.execPath, [join(here, script), ...argv], {
      cwd: tmp, env: { PATH: process.env.PATH, RPC_URL: fork.url, PRIVATE_KEY: key, POOL_ALLOW_FORK: "1", DOTENV_CONFIG_PATH: "/dev/null" },
    });
    let stdout = "", stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
  const planFile = join(root, "launch/tokenomics.testnet.json"), poolFile = join(root, "launch/pool.testnet.json");

  const launch = await run("launch.mjs", [planFile], deployerKey);
  check("launch.mjs deploys, splits the supply and hands over control", launch.status === 0, (launch.stderr + launch.stdout).slice(-600));
  const report = JSON.parse(readFileSync(join(tmp, "launch.31337.json"), "utf8"));
  const token = new ethers.Contract(report.ComputeCoinToken, artifact("ComputeCoinToken", "ComputeCoinToken").abi, provider);
  const settlement = new ethers.Contract(report.ComputeSettlement, artifact("ComputeSettlement", "ComputeSettlement").abi, provider);
  const supply = await token.totalSupply();
  check("the deployer ends with no tokens and no ownership", (await token.balanceOf(deployer)) === 0n && (await token.owner()) !== deployer && (await settlement.owner()) !== deployer);
  check("the multisig owns the token and the timelock owns settlement", (await token.owner()) === plan.wallets.SAFE && (await settlement.owner()) === report.ComputeTimelock);
  check("the liquidity wallet holds exactly the 10% share", (await token.balanceOf(liqAddr)) === supply / 10n);

  const dry = await run("pool.mjs", [poolFile, "--report", join(tmp, "launch.31337.json")], liqKey);
  check("pool.mjs dry run passes and sends nothing", dry.status === 0 && /DRY RUN/.test(dry.stdout), (dry.stderr + dry.stdout).slice(-500));
  const wrong = await run("pool.mjs", [poolFile, "--report", join(tmp, "launch.31337.json")], deployerKey);
  check("pool.mjs refuses the deployer's key (it must be the liquidity wallet)", wrong.status === 2 && /LIQUIDITY_WALLET/.test(wrong.stderr));
  const exec = await run("pool.mjs", [poolFile, "--report", join(tmp, "launch.31337.json"), "--execute"], liqKey);
  check("pool.mjs --execute creates the PancakeSwap pool", exec.status === 0 && /Pool created/.test(exec.stdout), (exec.stderr + exec.stdout).slice(-600));
  const pool = JSON.parse(readFileSync(join(tmp, "pool.31337.json"), "utf8"));
  const pair = new ethers.Contract(pool.pair, PAIR_ABI, provider);
  check("the pool holds 10,000,000 COMPUTE and 0.05 tBNB", pool.tokenReserve === ethers.parseEther("10000000").toString() && pool.nativeReserve === ethers.parseEther("0.05").toString());
  check("all LP tokens are in the 1-month lock, none with the liquidity wallet", (await pair.balanceOf(pool.lpLock)) === (await pair.totalSupply()) - 1000n && (await pair.balanceOf(liqAddr)) === 0n);

  // A stranger trades against it.
  const buyer = ethers.Wallet.createRandom().connect(provider);
  await provider.send("hardhat_setBalance", [buyer.address, "0x" + ethers.parseEther("1").toString(16)]);
  const router = new ethers.Contract(dex.router, ROUTER_ABI, buyer);
  const dl = (await provider.getBlock("latest")).timestamp + 600;
  await (await router.swapExactETHForTokensSupportingFeeOnTransferTokens(0, [dex.wnative, report.ComputeCoinToken], buyer.address, dl, { value: ethers.parseEther("0.001") })).wait();
  check("a stranger can buy COMPUTE with test BNB", (await token.balanceOf(buyer.address)) > 0n);

  // The lock holds for a month, then opens to the multisig.
  const st = await lockStatus(provider, pool.lpLock, pool.pair);
  check("the lock pays the multisig and nothing is releasable yet", st.beneficiary === plan.wallets.SAFE && st.releasable === 0n);
  await provider.send("evm_increaseTime", [31 * 86400]); await provider.send("evm_mine", []);
  const lock = new ethers.Contract(pool.lpLock, artifact("ComputeGovernance", "ComputeVesting").abi, buyer);
  await (await lock["release(address)"](pool.pair)).wait();
  check("after the month release() hands every LP token to the multisig", (await pair.balanceOf(plan.wallets.SAFE)) === (await pair.totalSupply()) - 1000n);

  // The beginner path: a single-key token with no launch report, pooled with --token (test networks only).
  const art = artifact("ComputeCoinToken", "ComputeCoinToken");
  const liqSigner = new ethers.Wallet(liqKey, provider);
  const quick = await new ethers.ContractFactory(art.abi, art.bytecode, liqSigner).deploy(liqAddr);
  await quick.waitForDeployment();
  const quickPool = { dex: "pancake-v2-bsc-testnet", tokenAmount: "1000", nativeAmount: "0.01", lp: { mode: "burn" } };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(join(tmp, "quick-pool.json"), JSON.stringify(quickPool));
  const q = await run("pool.mjs", [join(tmp, "quick-pool.json"), "--token", await quick.getAddress(), "--execute"], liqKey);
  check("pool.mjs --token pools a single-key test token and burns the LP tokens", q.status === 0 && /Pool created/.test(q.stdout), (q.stderr + q.stdout).slice(-500));

  const ver = await run("verify-launch.mjs", [join(tmp, "launch.31337.json"), "--pool", join(tmp, "pool.31337.json")], deployerKey);
  check("verify-launch.mjs rebuilds every contract's explorer input and it matches the chain", ver.status === 0 && !/\[FAIL\]/.test(ver.stdout), (ver.stderr + ver.stdout).slice(-500));
} finally { fork.stop(); }
console.log(failed === 0 ? "\nTESTNET DRESS REHEARSAL PASSED: only test BNB in the deployer is missing" : `\n${failed} CHECK(S) FAILED`);
process.exit(failed === 0 ? 0 : 1);
