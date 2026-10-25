/**
 * Launch the contracts per a written allocation plan (see launch/tokenomics.example.json).
 *
 *   DOTENV_CONFIG_PATH=testnet.env node scripts/launch.mjs launch/tokenomics.json
 *
 * Reads RPC_URL and PRIVATE_KEY from the environment. Refuses a mainnet chain unless the plan's
 * status is APPROVED and I_UNDERSTAND_MAINNET=yes. Replaces scripts/deploy-testnet.mjs when you
 * want the supply split and the contracts handed to a multisig/timelock.
 */
import "dotenv/config";
import { ethers } from "ethers";
import { readFileSync, writeFileSync } from "node:fs";
import { runLaunch } from "./lib/launch.mjs";

const planPath = process.argv[2];
if (!planPath) throw new Error("usage: node scripts/launch.mjs <plan.json>");
const need = (k) => { if (!process.env[k]) throw new Error(`Missing env ${k}`); return process.env[k]; };

const plan = JSON.parse(readFileSync(planPath, "utf8"));
const provider = new ethers.JsonRpcProvider(need("RPC_URL"));
const signer = new ethers.NonceManager(new ethers.Wallet(need("PRIVATE_KEY"), provider));
const net = await provider.getNetwork();
const bal = await provider.getBalance(await signer.getAddress());
console.log(`Network ${net.chainId}, deployer ${await signer.getAddress()}, balance ${ethers.formatEther(bal)}`);
if (bal === 0n) throw new Error("Deployer balance is 0: fund it from a testnet faucet first");

const { report } = await runLaunch({ signer, plan, log: console.log });
const out = `launch.${report.chainId}.json`;
writeFileSync(out, JSON.stringify(report, null, 2));
console.log(`\nDone. Saved ${out}`);
