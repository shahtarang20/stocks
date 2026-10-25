// Wallet page, end to end: a real browser, the real API, a real local chain with the real
// token deployed, and a mock browser wallet that signs with a real private key.
import { setBefore, launch, login, settle, WEB, API, S, T, save, body, q, apiLogin, api, newUser } from "./lib.mjs";
import { ethers } from "ethers";
import { readFileSync } from "fs";

const G = "wallet";
const chain = JSON.parse(readFileSync(S + "/chain.json", "utf8"));
const RPC = "http://127.0.0.1:8545";
const HH_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // public dev key, local chain only
const provider = new ethers.JsonRpcProvider(RPC);
const deployer = new ethers.NonceManager(new ethers.Wallet(HH_KEY, provider));
const token = new ethers.Contract(chain.ComputeCoinToken, ["function transfer(address,uint256) returns (bool)", "function balanceOf(address) view returns (uint256)"], deployer);
const b = await launch();

/** A page whose window.ethereum is backed by `wallet` (a real ethers key). */
async function walletPage(email, wallet, opts = {}) {
  const pg = await login(b, email);
  const o = { chainHex: "0x7a69", ...opts };
  await pg.exposeFunction("__wallet", async (method, params) => {
    const err = (code, message) => ({ __error: true, code, message });
    switch (method) {
      case "eth_requestAccounts": return o.rejectConnect ? err(4001, "User rejected the request.") : { result: [wallet.address.toLowerCase()] };
      case "eth_chainId": return { result: o.chainHex };
      case "wallet_switchEthereumChain": if (o.failSwitch) return err(4001, "User rejected the request."); o.chainHex = params[0].chainId; return { result: null };
      case "personal_sign": return o.rejectSign ? err(4001, "User rejected the request.") : { result: await wallet.signMessage(ethers.getBytes(params[0])) };
      default: return err(-32601, "unsupported " + method);
    }
  });
  await pg.addInitScript(() => {
    window.ethereum = {
      request: async ({ method, params }) => {
        const r = await window.__wallet(method, params || []);
        if (r && r.__error) { const e = new Error(r.message); e.code = r.code; throw e; }
        return r.result;
      },
    };
  });
  return pg;
}
const open = async (pg) => { await pg.goto(WEB + "/dashboard/wallet", { waitUntil: "networkidle", timeout: 60000 }); await settle(pg, 1200); };
const link = async (pg) => { await pg.click('button:has-text("Connect and link wallet")'); await pg.waitForTimeout(3500); };
const done = async (pg) => pg.context().close();

await T(G, "wallet page: shows simulated credits and the no-monetary-value notice before linking", async () => {
  const e = await newUser(b, "wal1"); const pg = await login(b, e); await open(pg);
  const t = await body(pg); const a = (await api(await apiLogin(e), "GET", "/v1/billing/account")).json;
  const ok = /Platform credits/.test(t) && /Test network, no monetary value/.test(t) && t.includes(a.available) && (await pg.locator('button:has-text("Connect and link wallet")').count()) === 1;
  await done(pg); return ok;
});
await T(G, "wallet page: with no browser wallet installed it explains what to do", async () => {
  const e = await newUser(b, "wal2"); const pg = await login(b, e); await open(pg);
  const t = await body(pg); await done(pg); return /No browser wallet detected/.test(t);
});

const w = ethers.Wallet.createRandom();
const email = await newUser(b, "wal3");
let pg = await walletPage(email, w); await open(pg);
await T(G, "link: connects, switches network, signs and shows the linked address", async () => {
  await link(pg);
  const shown = await pg.locator('[data-testid="wallet-address"]').innerText();
  return shown === w.address && q(`select count(*) from linked_wallets where address='${w.address.toLowerCase()}'`) === "1";
});
await T(G, "link: nothing was sent on-chain and the wallet needed no funds", async () => {
  return (await provider.getBalance(w.address)) === 0n && (await provider.getTransactionCount(w.address)) === 0;
});
await T(G, "balance: a wallet that holds no tokens shows 0 COMPUTE", async () => /0\.000000 COMPUTE/.test(await pg.locator('[data-testid="token-balance"]').innerText()));
await T(G, "balance: tokens received on-chain appear after Refresh, matching the chain exactly", async () => {
  await (await token.transfer(w.address, ethers.parseUnits("1234.5", 18))).wait();
  await pg.click('main button:has-text("Refresh")'); await pg.waitForTimeout(2500);
  const shown = await pg.locator('[data-testid="token-balance"]').innerText();
  const onChain = ethers.formatUnits(await token.balanceOf(w.address), 18);
  return /1234\.500000 COMPUTE/.test(shown) && Number(onChain) === 1234.5;
});
await T(G, "balance: a second transfer is reflected (no stale cache)", async () => {
  await (await token.transfer(w.address, ethers.parseUnits("0.25", 18))).wait();
  await pg.click('main button:has-text("Refresh")'); await pg.waitForTimeout(2500);
  return /1234\.750000 COMPUTE/.test(await pg.locator('[data-testid="token-balance"]').innerText());
});
await T(G, "link: persists after a reload and shows the token contract", async () => {
  await pg.reload({ waitUntil: "networkidle" }); await settle(pg, 1500);
  const t = await body(pg);
  return t.includes(w.address) && t.toLowerCase().includes(chain.ComputeCoinToken.toLowerCase()) && /1234\.750000/.test(t);
});
await T(G, "credits and tokens stay separate: linking changed no ledger balance", async () => {
  const a = (await api(await apiLogin(email), "GET", "/v1/billing/account")).json;
  return (await pg.locator('[data-testid="credits-available"]').innerText()) === a.available && a.available === "1000.00";
});
await T(G, "sidebar: Wallet is reachable from the customer navigation", async () => {
  await pg.goto(WEB + "/dashboard", { waitUntil: "networkidle", timeout: 60000 }); await settle(pg, 1200);
  await pg.click('aside a:has-text("Wallet")'); await pg.waitForURL(/dashboard\/wallet/, { timeout: 20000 }); return true;
});
await T(G, "unlink: removes the wallet and hides the balance", async () => {
  await open(pg); await pg.click('button:has-text("Unlink wallet")'); await pg.waitForTimeout(2000);
  return (await pg.locator('button:has-text("Connect and link wallet")').count()) === 1 && q(`select count(*) from linked_wallets where address='${w.address.toLowerCase()}'`) === "0";
});
await T(G, "unlink: the same wallet can be linked again", async () => { await link(pg); return (await pg.locator('[data-testid="wallet-address"]').innerText()) === w.address; });
await done(pg);

await T(G, "refusal: declining the connect prompt says so and links nothing", async () => {
  const e = await newUser(b, "wal4"); const x = ethers.Wallet.createRandom(); const p2 = await walletPage(e, x, { rejectConnect: true }); await open(p2); await link(p2);
  const ok = /You declined the request in your wallet/.test(await body(p2)) && q(`select count(*) from linked_wallets where address='${x.address.toLowerCase()}'`) === "0";
  await done(p2); return ok;
});
await T(G, "refusal: declining the signature says so and links nothing", async () => {
  const e = await newUser(b, "wal5"); const x = ethers.Wallet.createRandom(); const p2 = await walletPage(e, x, { rejectSign: true }); await open(p2); await link(p2);
  const ok = /You declined the request in your wallet/.test(await body(p2)) && q(`select count(*) from linked_wallets where address='${x.address.toLowerCase()}'`) === "0";
  await done(p2); return ok;
});
await T(G, "refusal: a wallet that will not switch to the right network gives a clear error", async () => {
  const e = await newUser(b, "wal6"); const x = ethers.Wallet.createRandom(); const p2 = await walletPage(e, x, { chainHex: "0x1", failSwitch: true }); await open(p2); await link(p2);
  const ok = /declined|network/i.test(await body(p2)) && q(`select count(*) from linked_wallets where address='${x.address.toLowerCase()}'`) === "0";
  await done(p2); return ok;
});
await T(G, "ownership: an address already linked to another account is refused in plain English", async () => {
  const e = await newUser(b, "wal7"); const p2 = await walletPage(e, w); await open(p2); await link(p2);
  const ok = /already linked to a different account/.test(await body(p2)); await done(p2); return ok;
});
await T(G, "ownership: an account cannot link a second wallet on the same network", async () => {
  const e = await newUser(b, "wal8"); const x = ethers.Wallet.createRandom(), y = ethers.Wallet.createRandom();
  const p2 = await walletPage(e, x); await open(p2); await link(p2); await p2.click('button:has-text("Unlink wallet")'); await p2.waitForTimeout(1500);
  const tk = await apiLogin(e); // first wallet is gone; link x via API then try y through the UI
  const ch = await api(tk, "POST", "/v1/wallet/link-challenge", { address: x.address });
  await api(tk, "POST", "/v1/wallet/link", { address: x.address, message: ch.json.message, signature: await x.signMessage(ch.json.message) });
  await done(p2);
  const p3 = await walletPage(e, y); await open(p3); const hasBtn = await p3.locator('button:has-text("Connect and link wallet")').count();
  await done(p3); return hasBtn === 0; // the page shows the linked wallet instead of offering a second link
});
await T(G, "verification: an unverified account is told to verify its email first", async () => {
  const em = `walu-${Math.random().toString(36).slice(2, 7)}@example.com`;
  await fetch(API + "/auth/register", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: em, password: "Password123!" }) });
  const x = ethers.Wallet.createRandom(); const p2 = await walletPage(em, x); await open(p2); await link(p2);
  const ok = /Verify your email first/.test(await body(p2)); await done(p2); return ok;
});
await T(G, "provider navigation: hosts also reach the Wallet page", async () => {
  const p2 = await login(b, "prov1@example.com"); await p2.goto(WEB + "/provider/dashboard", { waitUntil: "networkidle", timeout: 60000 }); await settle(p2, 1200);
  await p2.click('aside a:has-text("Wallet")'); await p2.waitForURL(/dashboard\/wallet/, { timeout: 20000 }); await done(p2); return true;
});
await T(G, "security: the API refuses a signature made by a different key through the same UI path", async () => {
  const e = await newUser(b, "wal9"); const tk = await apiLogin(e); const owner = ethers.Wallet.createRandom(), attacker = ethers.Wallet.createRandom();
  const ch = await api(tk, "POST", "/v1/wallet/link-challenge", { address: owner.address });
  const r = await api(tk, "POST", "/v1/wallet/link", { address: owner.address, message: ch.json.message, signature: await attacker.signMessage(ch.json.message) });
  return r.status === 400 && r.json.detail === "signature_wrong_signer" && q(`select count(*) from linked_wallets where address='${owner.address.toLowerCase()}'`) === "0";
});

save("w1.json"); await b.close();
