/**
 * Starts a local fork of a BNB Smart Chain network and returns its RPC URL. Public nodes rate-limit and
 * prune old state, so reads go through a small local proxy that caches every answer for the pinned block
 * and retries across several endpoints. Spends nothing and sends nothing to the real network.
 */
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export const DEFAULT_UPSTREAMS = {
  mainnet: ["https://bsc-rpc.publicnode.com", "https://rpc-bsc.48.club", "https://bsc-mainnet.public.blastapi.io"],
  testnet: ["https://bsc-testnet-rpc.publicnode.com", "https://data-seed-prebsc-1-s1.binance.org:8545", "https://data-seed-prebsc-2-s1.binance.org:8545"],
};

export async function startFork({ target = "mainnet", upstreams, port = "8599", blocksBehind = 30, log = console.log } = {}) {
  upstreams = upstreams ?? (process.env.FORK_URLS ? process.env.FORK_URLS.split(",") : DEFAULT_UPSTREAMS[target]);
  const cache = new Map();

  async function upstream(req) {
    const key = JSON.stringify({ m: req.method, p: req.params });
    if (cache.has(key)) return { ...cache.get(key), id: req.id };
    let lastErr = "no upstream answered";
    for (let attempt = 0; attempt < 12; attempt++) {
      const url = upstreams[attempt % upstreams.length];
      try {
        const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(req), signal: AbortSignal.timeout(15000) });
        if (r.ok) {
          const j = await r.json();
          const transient = j.error && /rate|limit|too many|missing trie|not available|not supported|header not found|timeout|pruned|state.*not/i.test(JSON.stringify(j.error));
          if (!transient) { cache.set(key, { jsonrpc: "2.0", result: j.result, error: j.error }); return j; }
          lastErr = JSON.stringify(j.error);
        } else lastErr = `${url} HTTP ${r.status}`;
      } catch (e) { lastErr = `${url} ${e.message}`; }
      await sleep(150 * (attempt + 1));
    }
    return { jsonrpc: "2.0", id: req.id, error: { code: -32000, message: `proxy gave up: ${lastErr}` } };
  }

  const proxy = createServer((req, res) => {
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", async () => {
      const q = JSON.parse(body);
      const out = Array.isArray(q) ? await Promise.all(q.map(upstream)) : await upstream(q);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(out));
    });
  });
  await new Promise((r) => proxy.listen(0, "127.0.0.1", r));
  const forkUrl = `http://127.0.0.1:${proxy.address().port}`;

  let block = process.env.FORK_BLOCK;
  if (!block) {
    const head = await upstream({ jsonrpc: "2.0", id: 1, method: "eth_blockNumber", params: [] });
    block = String(parseInt(head.result, 16) - blocksBehind);
  }
  log(`Forking BNB Smart Chain ${target} at block ${block} (via a caching proxy over ${upstreams.length} public endpoints) on port ${port}…`);
  const node = spawn(join(root, "node_modules", ".bin", "hardhat"), ["node", "--fork", forkUrl, "--fork-block-number", block, "--port", port], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  node.stdout.on("data", (d) => (output += d));
  node.stderr.on("data", (d) => (output += d));
  const stop = () => { try { node.kill("SIGTERM"); } catch {} try { proxy.close(); } catch {} };
  process.on("exit", stop);
  process.on("SIGINT", () => { stop(); process.exit(130); });

  const rpc = async (method) => {
    const r = await fetch(`http://127.0.0.1:${port}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: [] }) });
    return (await r.json()).result;
  };
  let up = false;
  for (let i = 0; i < 90 && !up; i++) {
    await sleep(1000);
    if (node.exitCode !== null) { stop(); throw new Error(`the fork node exited:\n${output.slice(0, 1500)}`); }
    up = Boolean(await rpc("eth_chainId").catch(() => null));
  }
  if (!up) { stop(); throw new Error("the fork node did not start within 90 seconds"); }
  return { url: `http://127.0.0.1:${port}`, stop };
}
