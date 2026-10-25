/**
 * Moves test coins from the deployer to another wallet (e.g. the liquidity wallet) on a TEST network,
 * so one faucet request can fund the whole rehearsal.
 *
 *   DOTENV_CONFIG_PATH=testnet.env node scripts/testnet-fund.mjs <to-address> <amount>
 *   e.g. ... node scripts/testnet-fund.mjs 0x45E6...3B3F 0.1
 *
 * Refuses any mainnet chain.
 */
import "dotenv/config";
import { ethers } from "ethers";
import { MAINNET_CHAIN_IDS } from "./lib/launch.mjs";

const [to, amount] = process.argv.slice(2);
if (!ethers.isAddress(to ?? "") || !/^\d+(\.\d+)?$/.test(amount ?? "")) throw new Error("usage: node scripts/testnet-fund.mjs <to-address> <amount>");
const provider = new ethers.JsonRpcProvider(process.env.RPC_URL);
const net = await provider.getNetwork();
if (MAINNET_CHAIN_IDS.has(net.chainId)) throw new Error(`refusing to move funds on mainnet chain ${net.chainId}; this tool is for test networks`);
const wallet = new ethers.Wallet(process.env.PRIVATE_KEY, provider);
const value = ethers.parseEther(amount);
const bal = await provider.getBalance(wallet.address);
if (bal < value + ethers.parseEther("0.002")) throw new Error(`${wallet.address} holds ${ethers.formatEther(bal)}; not enough for ${amount} plus gas`);
const tx = await wallet.sendTransaction({ to, value });
await tx.wait();
console.log(`Sent ${amount} to ${to} on chain ${net.chainId}: ${tx.hash}`);
