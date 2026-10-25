// Run with: npm run test:js   (no network, no chain)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ethers } from "ethers";
import { parseAmount, validatePoolConfig, planPool, assertMainnetConfirmation, resolveDex, isMainnet, PoolError, DEX_PRESETS } from "./pool.mjs";
import { validatePlan, PlanError } from "./launch.mjs";

const json = (p) => JSON.parse(readFileSync(new URL(p, import.meta.url), "utf8"));
const E = ethers.parseEther;
const good = { dex: "pancake-v2-bsc-testnet", tokenAmount: "10000000", nativeAmount: "0.05", lp: { mode: "burn" } };

test("amounts are strict decimal strings", () => {
  assert.equal(parseAmount("x", "1.5"), E("1.5"));
  for (const bad of [1, 1.5, "", "abc", "1e18", "-1", "0", "0.0", "1.0000000000000000001", " 1", "1,000", null, undefined]) {
    assert.throws(() => parseAmount("x", bad), PoolError, String(bad));
  }
});

test("a good config validates and defaults are applied", () => {
  const c = validatePoolConfig(good);
  assert.equal(c.slippageBps, 100);
  assert.equal(c.deadlineSeconds, 600);
  assert.equal(c.dex.chainId, 97);
});

test("bad configs are refused", () => {
  const bad = [
    { ...good, dex: "uniswap-v2" },
    { ...good, slippageBps: 0 }, { ...good, slippageBps: 501 }, { ...good, slippageBps: 1.5 },
    { ...good, deadlineSeconds: 5 }, { ...good, deadlineSeconds: 99999 },
    { ...good, lp: undefined }, { ...good, lp: { mode: "keep" } },
    { ...good, lp: { mode: "lock", lockMonths: 0, beneficiary: "SAFE" } },
    { ...good, lp: { mode: "lock", lockMonths: 12 } },
    { ...good, lp: { mode: "custom", recipient: "nope" } },
    null, "x",
  ];
  for (const b of bad) assert.throws(() => validatePoolConfig(b), PoolError, JSON.stringify(b));
});

test("price and slippage bounds are exact integer maths", () => {
  const p = planPool({ tokenAmount: E("100000000"), nativeAmount: E("5"), slippageBps: 100, totalSupply: E("1000000000") });
  assert.equal(p.minToken, E("99000000"));
  assert.equal(p.minNative, E("4.95"));
  assert.equal(p.nativePerToken, E("0.00000005"));
  assert.equal(p.tokensPerNative, E("20000000"));
  assert.equal(p.fdvNative, E("50")); // 1B tokens at 0.00000005 BNB
});

test("mainnet needs the flag and both amounts typed again", () => {
  const ok = { I_UNDERSTAND_MAINNET: "yes", CONFIRM_TOKEN_AMOUNT: "100", CONFIRM_NATIVE_AMOUNT: "2" };
  assert.doesNotThrow(() => assertMainnetConfirmation({ tokenAmountText: "100", nativeAmountText: "2", env: ok }));
  for (const drop of ["I_UNDERSTAND_MAINNET", "CONFIRM_TOKEN_AMOUNT", "CONFIRM_NATIVE_AMOUNT"]) {
    assert.throws(() => assertMainnetConfirmation({ tokenAmountText: "100", nativeAmountText: "2", env: { ...ok, [drop]: undefined } }), PoolError, drop);
  }
  assert.throws(() => assertMainnetConfirmation({ tokenAmountText: "100", nativeAmountText: "2", env: { ...ok, CONFIRM_NATIVE_AMOUNT: "20" } }), /CONFIRM_NATIVE_AMOUNT/);
});

test("mainnet is decided by the preset OR the chain id, so a fork cannot dodge the rules", () => {
  assert.equal(isMainnet(DEX_PRESETS["pancake-v2-bsc"], 31337), true);
  assert.equal(isMainnet({ mainnet: false }, 56), true);
  assert.equal(isMainnet(DEX_PRESETS["pancake-v2-bsc-testnet"], 97), false);
});

test("presets use valid checksummed addresses", () => {
  for (const k of Object.keys(DEX_PRESETS)) {
    const d = resolveDex(k);
    for (const a of [d.router, d.factory, d.wnative]) assert.equal(ethers.getAddress(a), a, `${k} ${a}`);
  }
});

test("the shipped plans are valid, and a mainnet plan needs APPROVED", () => {
  const example = json("../../launch/tokenomics.example.json");
  example.wallets = Object.fromEntries(Object.keys(example.wallets).map((k) => [k, ethers.Wallet.createRandom().address]));
  assert.throws(() => validatePlan(example, { chainId: 56 }), PlanError); // PLACEHOLDER on mainnet
  assert.doesNotThrow(() => validatePlan(example, { chainId: 97 }));
  const testnet = json("../../launch/tokenomics.testnet.json");
  assert.doesNotThrow(() => validatePlan(testnet, { chainId: 97 }));
  assert.equal(testnet.allocations.reduce((s, a) => s + a.bps, 0), 10000);
});

test("the shipped pool configs are valid", () => {
  assert.doesNotThrow(() => validatePoolConfig(json("../../launch/pool.testnet.json")));
  assert.doesNotThrow(() => validatePoolConfig(json("../../launch/pool.example.json")));
});
