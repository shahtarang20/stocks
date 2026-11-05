import { describe, expect, it } from "vitest";
import {
  PrivateStreamError,
  PrivateStreamReader,
  generateX25519Keypair,
  openSealed,
  padStreamPlaintext,
  seal,
  streamAad,
  unpadStreamPlaintext,
} from "./ccpp";
import { streamPrivateChat } from "./private-chat";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const JOB = "00000000-0000-4000-8000-0000000000a0";
const te = new TextEncoder();
const td = new TextDecoder();

function ev(pub: string, text: string, seq: number, final: boolean, o: { block?: number; job?: string; plain?: Uint8Array } = {}) {
  const job = o.job ?? JOB;
  const body = o.plain ?? padStreamPlaintext(te.encode(text), o.block ?? 256);
  return { object: "chat.completion.chunk.sealed", job_id: job, seq, final, sealed: seal(pub, body, streamAad(job, seq, final)) };
}
const sse = (events: unknown[]) => events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n";
const body = (text: string) =>
  new ReadableStream<Uint8Array>({
    start(c) {
      const b = te.encode(text);
      c.enqueue(b.slice(0, 23));
      c.enqueue(b.slice(23));
      c.close();
    },
  });
async function collect(it: AsyncIterable<string>) {
  let out = "";
  for await (const d of it) out += d;
  return out;
}

describe("unpadStreamPlaintext", () => {
  it("round trips and rejects bad frames", () => {
    expect(td.decode(unpadStreamPlaintext(padStreamPlaintext(te.encode("hello"), 256)))).toBe("hello");
    expect(padStreamPlaintext(new Uint8Array(300), 256).length).toBe(512);
    expect(() => unpadStreamPlaintext(new Uint8Array(2))).toThrow(PrivateStreamError);
    expect(() => unpadStreamPlaintext(new Uint8Array([0, 0, 0, 10, 97]))).toThrow(/exceeds/);
    const bad = padStreamPlaintext(te.encode("hi"), 16);
    bad[bad.length - 1] = 1;
    expect(() => unpadStreamPlaintext(bad)).toThrow(/non-zero/);
  });
});

describe("spec vectors fanout_v1/stream (when populated)", () => {
  const vectors = JSON.parse(readFileSync(join(process.cwd(), "../../docs/specs/ccpp-v1-vectors.json"), "utf8"));
  const sec = vectors.fanout_v1;
  const populated = Boolean(sec?.inputs && sec?.expected);
  it.skipIf(!populated)("opens and un-pads the generated chunk", () => {
    const i = sec.inputs.stream;
    const c = sec.expected.stream_chunk;
    const aad = streamAad(i.job_id, i.seq, i.final);
    expect(td.decode(aad)).toBe(c.aad_utf8);
    const padded = openSealed(i.recipient_private_hex, c.envelope, aad);
    expect(Array.from(padded).map((b) => b.toString(16).padStart(2, "0")).join("")).toBe(c.padded_plaintext_hex);
    expect(td.decode(unpadStreamPlaintext(padded))).toBe(i.text_utf8);
  });
  it.skipIf(populated)("not populated yet; round trips cover the format", () => {
    expect(populated).toBe(false);
  });
});

describe("PrivateStreamReader", () => {
  it("rejects reorder, replay, another job, flipped final, wrong key, bad padding, plaintext events, truncation", () => {
    const k = generateX25519Keypair();
    const e0 = ev(k.publicKeyHex, "a", 0, false);
    const e1 = ev(k.publicKeyHex, "b", 1, false);
    const e2 = ev(k.publicKeyHex, "c", 2, true);
    expect(() => new PrivateStreamReader(k.privateKeyHex).feed(e1)).toThrow(PrivateStreamError);
    const r = new PrivateStreamReader(k.privateKeyHex);
    expect(r.feed(e0)).toBe("a");
    expect(() => r.feed(e0)).toThrow(PrivateStreamError);
    expect(() => r.feed(ev(k.publicKeyHex, "b", 1, false, { job: "00000000-0000-4000-8000-0000000000b1" }))).toThrow(/another job/);
    expect(r.feed(e1)).toBe("b");
    expect(() => r.feed({ ...e2, final: false })).toThrow(PrivateStreamError);
    expect(() => r.close()).toThrow(/truncated/);
    expect(r.feed(e2)).toBe("c");
    expect(() => r.close()).not.toThrow();
    expect(() => new PrivateStreamReader(generateX25519Keypair().privateKeyHex).feed(e0)).toThrow(PrivateStreamError);
    const tampered = new Uint8Array(16);
    tampered.set([0, 0, 0, 2, 104, 105]);
    tampered[15] = 1;
    expect(() => new PrivateStreamReader(k.privateKeyHex).feed(ev(k.publicKeyHex, "", 0, true, { plain: tampered }))).toThrow(/non-zero/);
    expect(() => new PrivateStreamReader(k.privateKeyHex).feed({ object: "chat.completion.chunk" })).toThrow(PrivateStreamError);
    expect(() => new PrivateStreamReader(k.privateKeyHex).feed({ error: { code: "sealed_stream_incomplete" } })).toThrow(/sealed_stream_incomplete/);
  });
});

describe("streamPrivateChat", () => {
  const g1 = generateX25519Keypair();
  const g2 = generateX25519Keypair();
  const opts = { model: "m", messages: [{ role: "user", content: "secret question" }], guardians: { a: g1.publicKeyHex, b: g2.publicKeyHex }, threshold: 2, token: "t" };

  it("sends only ciphertext and yields decrypted deltas", async () => {
    let sent: any;
    const fetchImpl = (async (_u: string, init: RequestInit) => {
      sent = JSON.parse(init.body as string);
      const pub = sent.result_public_key;
      return new Response(body(sse([ev(pub, "Hi ", 0, false), ev(pub, "tére", 1, true)])), { status: 200 });
    }) as unknown as typeof fetch;
    expect(await collect(streamPrivateChat({ ...opts, fetchImpl, baseUrl: "https://api.test" }))).toBe("Hi tére");
    expect(sent.messages).toEqual([{ role: "user", content: "sealed" }]);
    expect(JSON.stringify(sent)).not.toContain("secret question");
    expect(sent.isolation_tier).toBe("sealed");
  });

  it("throws the server code when the flag is off, and on truncation", async () => {
    const off = (async () => new Response(JSON.stringify({ detail: "streaming_not_supported_for_private" }), { status: 400 })) as unknown as typeof fetch;
    await expect(collect(streamPrivateChat({ ...opts, fetchImpl: off, baseUrl: "https://x" }))).rejects.toThrow(/streaming_not_supported_for_private/);
    const trunc = (async (_u: string, init: RequestInit) => {
      const pub = JSON.parse(init.body as string).result_public_key;
      return new Response(body(sse([ev(pub, "a", 0, false)])), { status: 200 });
    }) as unknown as typeof fetch;
    await expect(collect(streamPrivateChat({ ...opts, fetchImpl: trunc, baseUrl: "https://x" }))).rejects.toThrow(/truncated/);
  });
});
