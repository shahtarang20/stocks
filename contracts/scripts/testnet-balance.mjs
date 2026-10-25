/**
 * Prints the deployer address and its native-coin balance on the configured testnet,
 * and says whether it can pay for a deployment yet.
 *
 *   DOTENV_CONFIG_PATH=testnet.env node scripts/testnet-balance.mjs
 */
import "dotenv/config";
import { ethers } from "ethers";

const rpc = process.env.RPC_URL;
const key = process.env.PRIVATE_KEY;
if (!rpc || !key) throw new Error("Set RPC_URL and PRIVATE_KEY (see testnet.env)");
const provider = new ethers.JsonRpcProvider(rpc);
const net = await provider.getNetwork();
const addr = new ethers.Wallet(key).address;
const bal = await provider.getBalance(addr);
console.log(`Network : chainId ${net.chainId}`);
console.log(`Deployer: ${addr}`);
console.log(`Balance : ${ethers.formatEther(bal)}`);
// Two contract deployments need roughly 0.02 of the native coin on BNB testnet.
console.log(bal >= ethers.parseEther("0.02") ? "READY: enough to deploy." : "NOT READY: fund this address from the testnet faucet first.");
