import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { ComputeCoinClient } from "./client.js";
import {
  INPUT_AAD,
  PrivateStreamError,
  PrivateStreamReader,
  buildPrivateKitInput,
  decryptData,
  generateX25519Keypair,
  hexToBytes,
  iterPrivateText,
  openSealed,
  openStreamChunk,
  padStreamPlaintext,
  preparePrivateFanoutParts,
  reconstructSecret,
  seal,
  shareAad,
  streamAad,
  unpadStreamPlaintext,
} from "./private.js";

const JOB = "00000000-0000-4000-8000-0000000000a0";
const te = new TextEncoder();
const td = new TextDecoder();

function sealedEvent(pub: string, text: string, seq: number, final: boolean, opts: { block?: number; job?: string; plain?: Uint8Array } = {}) {
  const job = opts.job ?? JOB;
  const body = opts.plain ?? padStreamPlaintext(te.encode(text), opts.block ?? 256);
  return { object: "chat.completion.chunk.sealed", job_id: job, seq, final, sealed: seal(pub, body, streamAad(job, seq, final)) };
}
const sseText = (events: unknown[]) => events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n";
const sseBody = (text: string): ReadableStream<Uint8Array> =>
  new ReadableStream({
    start(c) {
      const bytes = te.encode(text);
      // split mid-line to exercise buffering
      c.enqueue(bytes.slice(0, 17));
      c.enqueue(bytes.slice(17));
      c.close();
    },
  });
async function collect(it: AsyncIterable<string>): Promise<string> {
  let out = "";
  for await (const d of it) out += d;
  return out;
}

describe("unpadStreamPlaintext", () => {
  it("round trips and rejects bad frames", () => {
    expect(td.decode(unpadStreamPlaintext(padStreamPlaintext(te.encode("hello"), 256)))).toBe("hello");
    expect(unpadStreamPlaintext(padStreamPlaintext(new Uint8Array(0), 256)).length).toBe(0);
    expect(padStreamPlaintext(new Uint8Array(300), 256).length).toBe(512);
    expect(() => unpadStreamPlaintext(new Uint8Array(2))).toThrow(PrivateStreamError);
    expect(() => unpadStreamPlaintext(new Uint8Array([0, 0, 0, 10, 97, 98, 99]))).toThrow(/exceeds/);
    const bad = padStreamPlaintext(te.encode("hi"), 16);
    bad[bad.length - 1] = 1;
    expect(() => unpadStreamPlaintext(bad)).toThrow(/non-zero/);
  });
});

describe("spec vectors (fanout_v1/stream)", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const vectors = JSON.parse(readFileSync(resolve(here, "../../../docs/specs/ccpp-v1-vectors.json"), "utf8"));
  const sec = vectors.fanout_v1;
  const populated = Boolean(sec?.inputs && sec?.expected);
  it.skipIf(!populated)("opens and un-pads the generated stream chunk", () => {
    const i = sec.inputs.stream;
    const chunk = sec.expected.stream_chunk;
    const aad = streamAad(i.job_id, i.seq, i.final);
    expect(td.decode(aad)).toBe(chunk.aad_utf8);
    const padded = openSealed(i.recipient_private_hex, chunk.envelope, aad);
    expect(Buffer.from(padded).toString("hex")).toBe(chunk.padded_plaintext_hex);
    expect(td.decode(unpadStreamPlaintext(padded))).toBe(i.text_utf8);
    expect(openStreamChunk(i.recipient_private_hex, chunk.envelope, { jobId: i.job_id, seq: i.seq, final: i.final, padBytes: i.pad_bytes })).toBe(i.text_utf8);
  });
  it.skipIf(populated)("is not populated yet: the round trips below cover the format", () => {
    expect(populated).toBe(false);
  });
});

describe("PrivateStreamReader / iterPrivateText", () => {
  it("decrypts in order, including multibyte text split across SSE reads", async () => {
    const { privateKeyHex, publicKeyHex } = generateX25519Keypair();
    const evs = [sealedEvent(publicKeyHex, "Hel", 0, false), sealedEvent(publicKeyHex, "lo é世", 1, false), sealedEvent(publicKeyHex, "!", 2, true)];
    expect(await collect(iterPrivateText(sseBody(sseText(evs)), privateKeyHex))).toBe("Hello é世!");
  });

  it("handles an unpadded stream when padBytes is 0", async () => {
    const { privateKeyHex, publicKeyHex } = generateX25519Keypair();
    const evs = [sealedEvent(publicKeyHex, "ab", 0, false, { block: 0 }), sealedEvent(publicKeyHex, "c", 1, true, { block: 0 })];
    expect(await collect(iterPrivateText(sseBody(sseText(evs)), privateKeyHex, 0))).toBe("abc");
  });

  it("rejects reorder, replay, truncation, another job, flipped final, wrong key, bad padding and plaintext events", async () => {
    const { privateKeyHex, publicKeyHex } = generateX25519Keypair();
    const other = generateX25519Keypair();
    const e0 = sealedEvent(publicKeyHex, "a", 0, false);
    const e1 = sealedEvent(publicKeyHex, "b", 1, false);
    const e2 = sealedEvent(publicKeyHex, "c", 2, true);
    const run = (evs: unknown[]) => collect(iterPrivateText(sseBody(sseText(evs)), privateKeyHex));
    await expect(run([e1, e0, e2])).rejects.toThrow(PrivateStreamError);
    await expect(run([e0, e0])).rejects.toThrow(PrivateStreamError);
    await expect(run([e0, e1])).rejects.toThrow(/truncated/);
    await expect(run([e0, sealedEvent(publicKeyHex, "b", 1, false, { job: "00000000-0000-4000-8000-0000000000b1" })])).rejects.toThrow(/another job/);
    await expect(run([e0, e1, { ...e2, final: false }])).rejects.toThrow(PrivateStreamError);
    expect(() => new PrivateStreamReader(other.privateKeyHex).feed(e0)).toThrow(PrivateStreamError);
    const tampered = new Uint8Array(16);
    tampered.set([0, 0, 0, 2, 104, 105]);
    tampered[15] = 1;
    expect(() => new PrivateStreamReader(privateKeyHex).feed(sealedEvent(publicKeyHex, "", 0, true, { plain: tampered }))).toThrow(/non-zero/);
    expect(() => new PrivateStreamReader(privateKeyHex).feed({ object: "chat.completion.chunk", choices: [] })).toThrow(PrivateStreamError);
    expect(() => new PrivateStreamReader(privateKeyHex).feed({ error: { code: "sealed_stream_incomplete" } })).toThrow(/sealed_stream_incomplete/);
  });
});

describe("crypto self-consistency", () => {
  it("seal/open and Shamir round trip", () => {
    const kp = generateX25519Keypair();
    const env = seal(kp.publicKeyHex, te.encode("x"), te.encode("aad"));
    expect(td.decode(openSealed(kp.privateKeyHex, env, te.encode("aad")))).toBe("x");
    expect(() => openSealed(kp.privateKeyHex, env, te.encode("other"))).toThrow();
  });
});

describe("private Kit input and fan-out", () => {
  it("builds the documented plaintext shape", () => {
    expect(buildPrivateKitInput({ img: new Uint8Array([0, 1]), note: "hi", pre: { b64: "AAE=" }, t: { text: "x" } }, { n: 1 })).toEqual({
      inputs: { img: { b64: "AAE=" }, note: { text: "hi" }, pre: { b64: "AAE=" }, t: { text: "x" } },
      params: { n: 1 },
    });
    expect(buildPrivateKitInput()).toEqual({ inputs: {}, params: {} });
    expect(() => buildPrivateKitInput({ "../x": "a" })).toThrow();
    expect(() => buildPrivateKitInput({ ok: { b64: "!!" } })).toThrow();
  });

  it("gives every part its own keys and the guardians can recover each input", () => {
    const g = { g1: generateX25519Keypair(), g2: generateX25519Keypair(), g3: generateX25519Keypair() };
    const guardians = Object.fromEntries(Object.entries(g).map(([n, k]) => [n, k.publicKeyHex]));
    const plains = [buildPrivateKitInput({ f: "one" }, { i: 0 }), buildPrivateKitInput({ f: "two" }, { i: 1 })];
    const { parts, resultPrivateKeys } = preparePrivateFanoutParts(plains, guardians, 2);
    expect(new Set(resultPrivateKeys).size).toBe(2);
    expect(new Set(parts.map((p) => p.result_public_key)).size).toBe(2);
    parts.forEach((p, idx) => {
      expect(Object.keys(p).sort()).toEqual(["dk_shares", "encrypted_input", "result_public_key"]);
      const shares = (["g1", "g2"] as const).map((n) => ({
        x: p.dk_shares[n]!.x,
        share: openSealed(g[n].privateKeyHex, p.dk_shares[n]!.envelope, shareAad(n)),
      }));
      const dk = reconstructSecret(shares, 2);
      expect(JSON.parse(td.decode(decryptData(dk, p.encrypted_input, INPUT_AAD)))).toEqual(plains[idx]);
    });
  });

  it("submitPrivateFanout posts private_parts and returns the result keys", async () => {
    const guardians = { g1: generateX25519Keypair().publicKeyHex, g2: generateX25519Keypair().publicKeyHex };
    let seen: { url?: string; body?: any; idem?: string | null } = {};
    const fetchImpl = (async (url: string, init: RequestInit) => {
      if (url.endsWith("/v1/privacy/verifier-key")) return new Response(JSON.stringify({ guardians, threshold: 2 }), { status: 200 });
      seen = { url, body: JSON.parse(init.body as string), idem: new Headers(init.headers).get("Idempotency-Key") };
      return new Response(JSON.stringify({ part_count: 2 }), { status: 201 });
    }) as unknown as typeof fetch;
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });
    const sub = await client.submitPrivateFanout({
      spec: { kit: "k" },
      parts: [buildPrivateKitInput({ a: "1" }), buildPrivateKitInput({ a: "2" })],
      idempotencyKey: "idem-1",
    });
    expect(seen.url).toBe("https://api.test/v1/jobs/v2/fanout");
    expect(seen.idem).toBe("idem-1");
    expect(seen.body.isolation_tier).toBe("sealed");
    expect(seen.body.private_parts).toHaveLength(2);
    expect(sub.resultPrivateKeys).toHaveLength(2);
    await expect(client.submitPrivateFanout({ spec: {}, parts: [buildPrivateKitInput()], isolationTier: "container" as never })).rejects.toThrow();
  });
});

describe("client.chat.completions.create (stream)", () => {
  const guardians = { g1: generateX25519Keypair().publicKeyHex, g2: generateX25519Keypair().publicKeyHex };

  it("private: encrypts the messages and yields decrypted deltas", async () => {
    let sent: any;
    const fetchImpl = (async (url: string, init: RequestInit) => {
      if (url.endsWith("/v1/privacy/verifier-key")) return new Response(JSON.stringify({ guardians, threshold: 2 }), { status: 200 });
      sent = JSON.parse(init.body as string);
      const pub = sent.result_public_key as string;
      return new Response(sseBody(sseText([sealedEvent(pub, "Hi ", 0, false), sealedEvent(pub, "there", 1, true)])), { status: 200 });
    }) as unknown as typeof fetch;
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });
    const out = await collect(client.chat.completions.create({ model: "m", messages: [{ role: "user", content: "secret question" }], stream: true, isolation_tier: "sealed" }));
    expect(out).toBe("Hi there");
    expect(sent.messages).toEqual([{ role: "user", content: "sealed" }]);
    expect(JSON.stringify(sent)).not.toContain("secret question");
  });

  it("private: surfaces the server error when the streaming flag is off", async () => {
    const fetchImpl = (async (url: string) => {
      if (url.endsWith("/v1/privacy/verifier-key")) return new Response(JSON.stringify({ guardians, threshold: 2 }), { status: 200 });
      return new Response(JSON.stringify({ detail: "streaming_not_supported_for_private" }), { status: 400 });
    }) as unknown as typeof fetch;
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });
    await expect(collect(client.chat.completions.create({ model: "m", messages: [{ role: "user", content: "x" }], stream: true, isolation_tier: "sealed" }))).rejects.toThrow(/streaming_not_supported_for_private/);
  });

  it("non-private stream yields plain deltas and sends no encrypted fields", async () => {
    let sent: any;
    const fetchImpl = (async (_url: string, init: RequestInit) => {
      sent = JSON.parse(init.body as string);
      return new Response(sseBody(sseText([{ choices: [{ delta: { content: "a" } }] }, { choices: [{ delta: { content: "b" } }] }])), { status: 200 });
    }) as unknown as typeof fetch;
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });
    expect(await collect(client.chat.completions.create({ model: "m", messages: [{ role: "user", content: "x" }], stream: true }))).toBe("ab");
    expect(sent.encrypted_input).toBeUndefined();
  });
});

describe("private chat input contract", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const cases = JSON.parse(readFileSync(resolve(here, "../../../docs/specs/private-chat-input-vectors.json"), "utf8")).cases;

  it("matches the shared canonical vectors byte for byte (same file the Python SDK test uses)", async () => {
    const { buildPrivateChatInput, canonicalJson } = await import("./private.js");
    for (const c of cases) {
      const built = buildPrivateChatInput(c.messages, { max_tokens: c.max_tokens, temperature: c.temperature });
      expect(new TextDecoder().decode(canonicalJson(built))).toBe(c.expected);
    }
  });

  it("validates its input", async () => {
    const { buildPrivateChatInput } = await import("./private.js");
    expect(() => buildPrivateChatInput([])).toThrow();
    expect(() => buildPrivateChatInput([{ role: "user", content: "x" }], { max_tokens: 0 })).toThrow();
    expect(() => buildPrivateChatInput([{ role: "user", content: "x" }], { temperature: 3 })).toThrow();
  });
});
