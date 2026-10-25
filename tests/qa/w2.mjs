// Token deposits end to end: a customer links a wallet, sends REAL COMPUTE on a real local chain to the
// platform deposit address, the admin scans, and the credits show up in the customer's ledger.
import { launch, login, settle, WEB, API, S, T, save, body, q, apiLogin, api, newUser } from "./lib.mjs";
import { ethers } from "ethers";
import { readFileSync } from "fs";

const G = "deposits";
const chain = JSON.parse(readFileSync(S + "/chain.json", "utf8"));
const provider = new ethers.JsonRpcProvider("http://127.0.0.1:8545");
const HH_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
const DEPOSIT = "0xa0Ee7A142d267C1f36714E4a8F75612F20a79720";
const deployer = new ethers.NonceManager(new ethers.Wallet(HH_KEY, provider));
const erc20 = ["function transfer(address,uint256) returns (bool)", "function balanceOf(address) view returns (uint256)"];
const tokenAs = (signer) => new ethers.Contract(chain.ComputeCoinToken, erc20, signer);
const b = await launch();
const mine = async (n = 2) => { for (let i = 0; i < n; i++) await provider.send("evm_mine", []); };

async function fund(address, tokens) {
  await (await deployer.sendTransaction({ to: address, value: ethers.parseEther("1") })).wait();  // gas
  if (tokens) await (await tokenAs(deployer).transfer(address, ethers.parseUnits(String(tokens), 18))).wait();
}
async function walletPage(email, wallet) {
  const pg = await login(b, email);
  await pg.exposeFunction("__wallet", async (method, params) => {
    const err = (code, message) => ({ __error: true, code, message });
    switch (method) {
      case "eth_requestAccounts": return { result: [wallet.address.toLowerCase()] };
      case "eth_chainId": return { result: "0x7a69" };
      case "wallet_switchEthereumChain": return { result: null };
      case "personal_sign": return { result: await wallet.signMessage(ethers.getBytes(params[0])) };
      default: return err(-32601, "unsupported " + method);
    }
  });
  await pg.addInitScript(() => { window.ethereum = { request: async ({ method, params }) => { const r = await window.__wallet(method, params || []); if (r && r.__error) { const e = new Error(r.message); e.code = r.code; throw e; } return r.result; } }; });
  return pg;
}
const open = async (pg, path = "/dashboard/wallet") => { await pg.goto(WEB + path, { waitUntil: "networkidle", timeout: 60000 }); await settle(pg, 1500); };
const done = (pg) => pg.context().close();

const key = ethers.Wallet.createRandom(); const wallet = new ethers.Wallet(key.privateKey, provider);
const email = await newUser(b, "dep1");
await fund(wallet.address, 6000);
const cust = await walletPage(email, wallet); await open(cust);

await T(G, "deposit card: appears once the server has deposits enabled, with rate, limits and confirmations", async () => {
  const t = await body(cust);
  return /Deposit COMPUTE for credits/.test(t) && /1 COMPUTE = 10 credits/.test(t) && /1 to 1000 COMPUTE per transfer/.test(t) && /1 block confirmations/.test(t);
});
await T(G, "deposit card: before linking a wallet it asks the user to link first and shows no address", async () => {
  const t = await body(cust); return /Link your wallet above first/.test(t) && !t.toLowerCase().includes(DEPOSIT.toLowerCase());
});
await T(G, "deposit card: after linking it shows the deposit address and a 'send only from your linked wallet' warning", async () => {
  await cust.click('button:has-text("Connect and link wallet")'); await cust.waitForTimeout(3500);
  const t = await body(cust);
  return t.toLowerCase().includes(DEPOSIT.toLowerCase()) && t.includes("Send only from") && t.includes(wallet.address) && /No deposits yet/.test(t);
});

const base = (await api(await apiLogin(email), "GET", "/v1/billing/account")).json.available;
await (await tokenAs(wallet).transfer(DEPOSIT, ethers.parseUnits("25", 18))).wait();   // a real on-chain deposit
await mine(2);

const adminPg = await login(b, "admin@example.com");
await open(adminPg, "/admin/token-deposits");
await T(G, "admin: the page shows reconciliation before any scan", async () => /Reconciliation/.test(await body(adminPg)) && (await adminPg.locator('[data-testid="rec-status"]').count()) === 1);
await T(G, "admin: 'Scan now' reads the real transfer and credits it", async () => {
  await adminPg.click('button:has-text("Scan now")'); await adminPg.waitForTimeout(3000);
  return /1 credited/.test(await adminPg.locator('[data-testid="scan-result"]').innerText());
});
await T(G, "admin: the deposit is listed as credited, and chain and ledger are reported as balanced", async () => {
  await adminPg.click('button:has-text("Refresh")'); await adminPg.waitForTimeout(2000);
  const t = await body(adminPg);
  return /25\.000000/.test(t) && /credited/.test(t) && /Balanced/.test(await adminPg.locator('[data-testid="rec-status"]').innerText());
});
await T(G, "customer: the credits arrived in the ledger at exactly 25 x 10 = 250.00", async () => {
  const after = (await api(await apiLogin(email), "GET", "/v1/billing/account")).json.available;
  return (parseFloat(after) - parseFloat(base)).toFixed(2) === "250.00";
});
await T(G, "customer: the wallet page shows the deposit and the new credit balance", async () => {
  await open(cust); const t = await body(cust);
  const credits = await cust.locator('[data-testid="credits-available"]').innerText();
  return /25\.000000 COMPUTE/.test(t) && /\+250\.00 credits/.test(t) && credits === (parseFloat(base) + 250).toFixed(2);
});
await T(G, "customer: the Billing ledger shows the credit", async () => {
  await open(cust, "/dashboard/billing"); return /\+250\.00/.test(await body(cust));
});
await T(G, "scanning again does not credit the same deposit twice", async () => {
  await adminPg.click('button:has-text("Scan now")'); await adminPg.waitForTimeout(3000);
  const t = await adminPg.locator('[data-testid="scan-result"]').innerText();
  const after = (await api(await apiLogin(email), "GET", "/v1/billing/account")).json.available;
  return /0 credited/.test(t) && (parseFloat(after) - parseFloat(base)).toFixed(2) === "250.00";
});

const stranger = new ethers.Wallet(ethers.Wallet.createRandom().privateKey, provider);
await fund(stranger.address, 20);
await (await tokenAs(stranger).transfer(DEPOSIT, ethers.parseUnits("10", 18))).wait(); await mine(2);
await T(G, "a transfer from an address nobody linked is held as 'unclaimed' and credits nothing", async () => {
  await adminPg.click('button:has-text("Scan now")'); await adminPg.waitForTimeout(3000);
  const t = await adminPg.locator('[data-testid="scan-result"]').innerText();
  const unclaimed = q("select count(*) from token_deposits where status='unclaimed'");
  return /1 unclaimed/.test(t) && unclaimed === "1" && q("select count(*) from token_deposits where status='unclaimed' and credits_minor is not null") === "0";
});
await T(G, "admin: the status filter finds the unclaimed transfer", async () => {
  await adminPg.selectOption("#deposit-filter", "unclaimed"); await adminPg.waitForTimeout(1800);
  const t = await body(adminPg); return /10\.000000/.test(t) && /sender not linked/.test(t) && !/25\.000000/.test(t);
});
await T(G, "admin: reconciliation still balances and counts the unclaimed tokens", async () => {
  await adminPg.click('button:has-text("Refresh")'); await adminPg.waitForTimeout(2000);
  const t = await body(adminPg); return /Balanced/.test(t) && /Unclaimed \(sender not linked\)\s*10/.test(t);
});
await (await tokenAs(wallet).transfer(DEPOSIT, ethers.parseUnits("2000", 18))).wait(); await mine(2);
await T(G, "a deposit above the maximum is rejected and credits nothing, and the customer sees why", async () => {
  await adminPg.click('button:has-text("Scan now")'); await adminPg.waitForTimeout(3000);
  await open(cust); const t = await body(cust);
  const after = (await api(await apiLogin(email), "GET", "/v1/billing/account")).json.available;
  return /rejected/.test(t) && /above maximum/.test(t) && (parseFloat(after) - parseFloat(base)).toFixed(2) === "250.00";
});
await T(G, "a customer cannot open the admin deposits page or call its API", async () => {
  const pg = await login(b, email); await pg.goto(WEB + "/admin/token-deposits", { waitUntil: "networkidle", timeout: 60000 }); await settle(pg, 1500);
  const t = await body(pg); await done(pg);
  const r = await api(await apiLogin(email), "POST", "/v1/admin/token-deposits/scan");
  return /Administrator access is required/.test(t) && !/Reconciliation/.test(t) && !/25\.000000/.test(t) && r.status === 403;
});
await T(G, "a customer sees only their own deposits (another user's wallet page is empty)", async () => {
  const e2 = await newUser(b, "dep2"); const pg = await login(b, e2); await open(pg); const t = await body(pg); await done(pg);
  return /Deposit COMPUTE for credits/.test(t) && !/25\.000000/.test(t);
});

save("w2.json"); await b.close();
