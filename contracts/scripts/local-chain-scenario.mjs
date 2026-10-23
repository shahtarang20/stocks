#!/usr/bin/env node
/**
 * Drives REAL on-chain state on a local hardhat node for the Python chain
 * reconciliation integration tests (apps/api/tests/test_d2_chain_recon_live.py).
 *
 * It does NOT deploy — run scripts/deploy-settlement.js first; this reads
 * deployments/localhost-settlement.json and then performs genuine
 * lockBudget / settleJob / refundJob transactions against the deployed
 * ComputeSettlement, signing settlement vouchers with the real platform signer
 * key exactly the way the production signer must.
 *
 * Prints a single JSON object on stdout describing everything it did.
 *
 * Usage:
 *   node scripts/local-chain-scenario.mjs            # rpc defaults to 127.0.0.1:8545
 *   RPC_URL=http://127.0.0.1:8545 node scripts/local-chain-scenario.mjs
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { ethers } from "ethers";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, "..");

// Standard, publicly-known hardhat dev keys. Local test chain ONLY.
const DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";

function artifact(name) {
  return JSON.parse(
    fs.readFileSync(path.join(root, "artifacts", "src", `${name}.sol`, `${name}.json`), "utf8")
  );
}

const rpcUrl = process.env.RPC_URL || "http://127.0.0.1:8545";
const provider = new ethers.JsonRpcProvider(rpcUrl);
// ethers caches eth_getTransactionCount for 250ms by default, which hands two
// back-to-back sends the same nonce. Disable it: correctness over round-trips.
provider.disableCcipRead = true;
provider._getConnection && (provider._getConnection.timeout = 30000);
provider.cacheTimeout = -1;

const deployment = JSON.parse(
  fs.readFileSync(path.join(root, "deployments", "localhost-settlement.json"), "utf8")
);

const deployer = new ethers.Wallet(DEPLOYER_KEY, provider);
// The deploy script sets platformSigner = deployer when no override is given.
const platformSignerKey = process.env.PLATFORM_SIGNER_PRIVATE_KEY || DEPLOYER_KEY;
const platformSigner = new ethers.Wallet(platformSignerKey, provider);

const deployerNM = new ethers.NonceManager(deployer);
const token = new ethers.Contract(deployment.tokenAddress, artifact("ComputeCoinToken").abi, deployerNM);
const settlementAbi = artifact("ComputeSettlement").abi;
const settlement = new ethers.Contract(deployment.settlementAddress, settlementAbi, deployerNM);

const onChainSigner = await settlement.platformSigner();
if (onChainSigner.toLowerCase() !== (await platformSigner.getAddress()).toLowerCase()) {
  throw new Error(
    `platformSigner mismatch: contract says ${onChainSigner}, we hold ${await platformSigner.getAddress()}`
  );
}

const accounts = await provider.send("eth_accounts", []);
const providerAddress = ethers.getAddress(accounts[9]);
const chainId = Number((await provider.getNetwork()).chainId);

// Hard stop: this script signs with a publicly-known hardhat dev key, so any
// value it touches on a real network is immediately stealable. RPC_URL is
// overridable, so refuse outright on anything that is not the local dev chain.
if (chainId !== 31337) {
  throw new Error(
    `refusing to run: this script uses a publicly-known dev key and is for the ` +
      `local hardhat chain (31337) only, but ${rpcUrl} reports chainId ${chainId}`
  );
}

const ONE = 10n ** 18n;
const FAR_EXPIRY = 2000000000; // year 2033

// Salted so the script is re-runnable against a chain that already holds
// escrows from a previous run ("Escrow already exists" is permanent per key).
const RUN_SALT = process.env.RUN_SALT || ethers.hexlify(ethers.randomBytes(8));

function jobIdFor(label) {
  return ethers.keccak256(ethers.toUtf8Bytes(`d2-live-${RUN_SALT}-${label}`));
}

async function settlementVoucher({ customer, jobId, finalCost, platformFee, nonce, expiry }) {
  // Must match ComputeSettlement.settlementVoucherHash byte-for-byte.
  const digest = ethers.solidityPackedKeccak256(
    ["address", "bytes32", "uint256", "uint256", "uint256", "uint256", "uint256", "address"],
    [customer, jobId, finalCost, platformFee, nonce, expiry, chainId, deployment.settlementAddress]
  );
  const onChainDigest = await settlement.settlementVoucherHash(
    customer, jobId, finalCost, platformFee, nonce, expiry
  );
  if (onChainDigest !== digest) {
    throw new Error(`voucher digest mismatch: contract=${onChainDigest} local=${digest}`);
  }
  return platformSigner.signMessage(ethers.getBytes(digest));
}

const scenarios = {};

async function lock(label, accountIndex, amount, deadlineOffsetSeconds) {
  const jobId = jobIdFor(label);
  const customerSigner = await provider.getSigner(accountIndex);
  const customer = ethers.getAddress(await customerSigner.getAddress());

  await (await token.transfer(customer, amount)).wait();
  await (await token.connect(customerSigner).approve(deployment.settlementAddress, amount)).wait();

  const now = (await provider.getBlock("latest")).timestamp;
  const deadline = now + deadlineOffsetSeconds;
  const tx = await settlement
    .connect(customerSigner)
    .lockBudget(jobId, providerAddress, amount, deadline);
  const receipt = await tx.wait();

  scenarios[label] = {
    label,
    job_bytes32: jobId,
    escrow_id: await settlement.getEscrowId(customer, jobId),
    customer_address: customer,
    provider_address: providerAddress,
    locked_amount_units: amount.toString(),
    deadline,
    status: "locked",
    lock_tx_hash: receipt.hash,
    lock_block: receipt.blockNumber,
  };
  return { jobId, customer, customerSigner };
}

async function settle(label, finalCost, platformFee) {
  const s = scenarios[label];
  const nonce = await settlement.escrowNonce(s.customer_address, s.job_bytes32);
  const signature = await settlementVoucher({
    customer: s.customer_address,
    jobId: s.job_bytes32,
    finalCost,
    platformFee,
    nonce,
    expiry: FAR_EXPIRY,
  });
  const receipt = await (
    await settlement.settleJob(
      s.customer_address, s.job_bytes32, finalCost, platformFee, nonce, FAR_EXPIRY, signature
    )
  ).wait();
  const log = receipt.logs.find((l) => l.address.toLowerCase() === deployment.settlementAddress.toLowerCase()
    && l.topics[0] === ethers.id("JobSettled(bytes32,address,uint256,uint256,uint256)"));
  s.status = "settled";
  s.final_cost_units = finalCost.toString();
  s.platform_fee_units = platformFee.toString();
  s.provider_payout_units = (finalCost - platformFee).toString();
  s.customer_refund_units = (BigInt(s.locked_amount_units) - finalCost).toString();
  s.settle_tx_hash = receipt.hash;
  s.voucher_nonce = Number(nonce);
  s.voucher_signature = signature;
  s.settled_log_topics = log ? log.topics : null;
  s.settled_log_data = log ? log.data : null;
}

async function refundAfterDeadline(label, accountIndex) {
  const s = scenarios[label];
  const customerSigner = await provider.getSigner(accountIndex);
  const receipt = await (
    await settlement.connect(customerSigner).refundJob(s.customer_address, s.job_bytes32, 0, 0, "0x")
  ).wait();
  s.status = "refunded";
  s.refund_tx_hash = receipt.hash;
  s.refunded_amount_units = s.locked_amount_units;
}

// --- the scenario -----------------------------------------------------------
// 1 matched LOCKED, 1 matched SETTLED, 1 matched REFUNDED, plus two escrows
// that exist on-chain with no off-chain row (one settled, one merely locked).
await lock("locked_match", 1, 1000n * ONE, 86400 * 30);
await lock("settled_match", 2, 2000n * ONE, 86400 * 30);
await lock("refunded_match", 3, 500n * ONE, 60);
await lock("orphan_settled", 4, 300n * ONE, 86400 * 30);
await lock("orphan_locked", 5, 700n * ONE, 86400 * 30);

await settle("settled_match", 1500n * ONE, 150n * ONE);
await settle("orphan_settled", 300n * ONE, 30n * ONE);

// Push past refunded_match's deadline so the customer self-refund path opens.
await provider.send("evm_increaseTime", [3600]);
await provider.send("evm_mine", []);
await refundAfterDeadline("refunded_match", 3);

process.stdout.write(
  JSON.stringify(
    {
      rpc_url: rpcUrl,
      run_salt: RUN_SALT,
      chain_id: chainId,
      settlement_address: deployment.settlementAddress,
      token_address: deployment.tokenAddress,
      platform_signer: onChainSigner,
      fee_treasury: deployment.feeTreasury,
      scenarios,
    },
    null,
    2
  ) + "\n"
);
