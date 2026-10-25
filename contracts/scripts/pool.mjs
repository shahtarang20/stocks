/**
 * Seed the first PancakeSwap pool for COMPUTE.
 *
 *   # 1. look, send nothing (the default):
 *   DOTENV_CONFIG_PATH=testnet.env node scripts/pool.mjs launch/pool.testnet.json --report launch.97.json
 *   # 2. really do it:
 *   DOTENV_CONFIG_PATH=testnet.env node scripts/pool.mjs launch/pool.testnet.json --report launch.97.json --execute
 *
 * Reads RPC_URL and PRIVATE_KEY from the environment. The key must belong to the plan's
 * LIQUIDITY_WALLET, because that wallet holds the COMPUTE for the pool.
 *
 * For a throwaway single-key token (scripts/deploy-token.js) on a TEST network there is no launch report:
 * pass --token <address> instead of --report. That form is refused on mainnet.
 *
 * Mainnet (BSC, chain 56) additionally needs: the launch report's status APPROVED,
 * I_UNDERSTAND_MAINNET=yes, and the two amounts typed again as CONFIRM_TOKEN_AMOUNT and
 * CONFIRM_NATIVE_AMOUNT. Use a private/MEV-protected RPC for RPC_URL on mainnet.
 *
 * POOL_ALLOW_FORK=1 skips the chain-id match so the script can be rehearsed on a local fork
 * of BSC mainnet. It never relaxes any mainnet safety rule.
 */
import "dotenv/config";
import { ethers } from "ethers";
import { readFileSync, writeFileSync } from "node:fs";
import { runPool, validatePoolConfig, PoolError, isMainnet } from "./lib/pool.mjs";

const args = process.argv.slice(2);
const configPath = args.find((a) => !a.startsWith("--"));
const flag = (name) => { const i = args.indexOf(name); return i === -1 ? undefined : args[i + 1]; };
const execute = args.includes("--execute");
const reportPath = flag("--report");
const tokenArg = flag("--token");
if (!configPath || (!reportPath && !tokenArg)) throw new Error("usage: node scripts/pool.mjs <pool.json> (--report <launch.<chainId>.json> | --token <address>) [--execute]");
const need = (k) => { if (!process.env[k]) throw new Error(`Missing env ${k}`); return process.env[k]; };

try {
  const raw = JSON.parse(readFileSync(configPath, "utf8"));
  const config = validatePoolConfig(raw);
  if (tokenArg && !ethers.isAddress(tokenArg)) throw new PoolError(`--token ${tokenArg} is not an address`);
  const report = reportPath ? JSON.parse(readFileSync(reportPath, "utf8")) : { chainId: undefined, ComputeCoinToken: ethers.getAddress(tokenArg), status: "UNPLANNED", wallets: {} };
  const provider = new ethers.JsonRpcProvider(need("RPC_URL"));
  const net = await provider.getNetwork();
  const mainnet = isMainnet(config.dex, net.chainId);

  if (tokenArg && mainnet) throw new PoolError("--token is for throwaway test tokens; a mainnet pool needs the launch report (--report) so the plan, wallets and approval are checked");
  if (!process.env.POOL_ALLOW_FORK && !tokenArg && Number(net.chainId) !== report.chainId) {
    throw new PoolError(`the launch report is for chain ${report.chainId} but RPC_URL is chain ${net.chainId}`);
  }
  if (mainnet && report.status !== "APPROVED") throw new PoolError(`launch report status is "${report.status}"; mainnet needs APPROVED`);

  const signer = new ethers.NonceManager(new ethers.Wallet(need("PRIVATE_KEY"), provider));
  const me = await signer.getAddress();
  const liquidityWallet = report.wallets?.LIQUIDITY_WALLET;
  if (liquidityWallet && liquidityWallet.toLowerCase() !== me.toLowerCase()) {
    throw new PoolError(`PRIVATE_KEY is ${me}, but the plan's LIQUIDITY_WALLET is ${liquidityWallet}`);
  }

  const { dryRun, summary } = await runPool({
    signer, token: report.ComputeCoinToken, config, wallets: report.wallets, execute,
    raw: { tokenAmount: raw.tokenAmount, nativeAmount: raw.nativeAmount },
    log: console.log, allowChainMismatch: Boolean(process.env.POOL_ALLOW_FORK),
  });

  if (dryRun) {
    console.log("\nDRY RUN: nothing was sent. Add --execute to create the pool.");
  } else {
    const out = `pool.${summary.chainId}.json`;
    writeFileSync(out, JSON.stringify(summary, null, 2));
    console.log(`\nPool created.\n  pair   ${summary.pair}\n  swap   ${summary.swapUrl}\n  scan   ${summary.explorerPair}\nSaved ${out}`);
  }
} catch (err) {
  if (err instanceof PoolError) { console.error(`\nREFUSED: ${err.message}`); process.exit(2); }
  throw err;
}
