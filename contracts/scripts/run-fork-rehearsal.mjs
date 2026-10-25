/**
 * Starts a local fork of BNB Smart Chain mainnet, runs the pool rehearsal against it, stops the fork.
 *
 *   node scripts/run-fork-rehearsal.mjs
 *
 * FORK_URLS   comma-separated BSC RPC URLs to read state from (default: three public endpoints)
 * FORK_BLOCK  block to fork from (default: 30 blocks behind the head, which public nodes still serve)
 * Needs network access to those RPCs, but spends nothing and sends nothing to mainnet.
 */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { startFork } from "./lib/fork.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const fork = await startFork({ target: "mainnet", port: process.env.FORK_PORT || "8599" });
const child = spawn(process.execPath, [join(here, "pool-fork-rehearsal.mjs")], { cwd: join(here, ".."), stdio: "inherit", env: { ...process.env, RPC_URL: fork.url } });
const code = await new Promise((r) => child.on("exit", r));
fork.stop();
process.exit(code ?? 1);
