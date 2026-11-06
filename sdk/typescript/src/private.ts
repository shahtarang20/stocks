/**
 * Customer-side Proof-of-Privacy primitives for the TypeScript SDK (ccpp-v1, Node 18+ only: uses `node:crypto`).
 *
 * Byte-compatible with sdk/python/computecoin/privacy_crypto.py and apps/web/src/lib/ccpp.ts: X25519 + HKDF-SHA256 +
 * ChaCha20-Poly1305 sealing, a symmetric data envelope, and Shamir sharing over GF(256) (poly 0x11D, generator 2).
 * Also: sealed private chat stream chunks with their padding frame, and the private Kit input/fan-out helpers.
 */

import {
  createCipheriv,
  createDecipheriv,
  createHash,
  createPrivateKey,
  createPublicKey,
  diffieHellman,
  generateKeyPairSync,
  hkdfSync,
  randomBytes,
} from "node:crypto";

export const VERSION = "ccpp-v1";
export const SEAL_ALG = "X25519-HKDF-SHA256-ChaCha20Poly1305";
export const DATA_ALG = "ChaCha20Poly1305";
export const DEFAULT_PAD_BYTES = 256;

export interface SealedEnvelope {
  v: string;
  alg: string;
  epk: string;
  nonce: string;
  ct: string;
}
export interface DataEnvelope {
  v: string;
  alg: string;
  nonce: string;
  ct: string;
}
export interface PrivatePart {
  encrypted_input: DataEnvelope;
  dk_shares: Record<string, { x: number; envelope: SealedEnvelope }>;
  result_public_key: string;
}

export class PrivacyCryptoError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PrivacyCryptoError";
  }
}
/** A sealed stream chunk is malformed, out of order, does not open, or the stream is truncated. */
export class PrivateStreamError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PrivateStreamError";
  }
}

// --------------------------------------------------------------------------- helpers
const enc = new TextEncoder();

export function hexToBytes(hex: string): Uint8Array {
  if (typeof hex !== "string" || hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) {
    throw new PrivacyCryptoError("invalid hex string");
  }
  return new Uint8Array(Buffer.from(hex, "hex"));
}
export function bytesToHex(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("hex");
}
function hex32(hex: string, what: string): Uint8Array {
  const b = hexToBytes(hex);
  if (b.length !== 32) throw new PrivacyCryptoError(`${what} must be 32 bytes`);
  return b;
}

const PKCS8_X25519 = Buffer.from("302e020100300506032b656e04220420", "hex");
const SPKI_X25519 = Buffer.from("302a300506032b656e032100", "hex");
const privObj = (raw: Uint8Array) => createPrivateKey({ key: Buffer.concat([PKCS8_X25519, raw]), format: "der", type: "pkcs8" });
const pubObj = (raw: Uint8Array) => createPublicKey({ key: Buffer.concat([SPKI_X25519, raw]), format: "der", type: "spki" });
const pubRawOf = (priv: ReturnType<typeof privObj>): Uint8Array =>
  new Uint8Array(createPublicKey(priv).export({ format: "der", type: "spki" }).subarray(-32));

/** `ensure_ascii`-style canonical JSON, same as Python `json.dumps(sort_keys=True, separators=(",", ":"))`.
 * Python escapes everything outside 0x20..0x7e, so DEL (0x7f) is escaped here too (W9: the bytes must not depend on the OS). */
export function canonicalJson(data: unknown): Uint8Array {
  const sort = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sort);
    if (v !== null && typeof v === "object") {
      const o: Record<string, unknown> = {};
      for (const k of Object.keys(v as object).sort()) o[k] = sort((v as Record<string, unknown>)[k]);
      return o;
    }
    return v;
  };
  const s = JSON.stringify(sort(data)).replace(/[\u007f-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  return enc.encode(s);
}

export const INPUT_AAD = enc.encode(`${VERSION}|job-input`);
export const shareAad = (guardian: string): Uint8Array => enc.encode(`${VERSION}|guardian-share|${guardian}`);
export const outputAad = (jobId: string): Uint8Array => enc.encode(`${VERSION}|job-output|${jobId}`);
export const streamAad = (jobId: string, seq: number, final: boolean): Uint8Array =>
  enc.encode(`${VERSION}|stream|${jobId}|${seq}|${final ? 1 : 0}`);

// --------------------------------------------------------------------------- AEAD
function aeadEncrypt(key: Uint8Array, nonce: Uint8Array, plaintext: Uint8Array, aad: Uint8Array): Uint8Array {
  const c = createCipheriv("chacha20-poly1305", key, nonce, { authTagLength: 16 });
  c.setAAD(aad, { plaintextLength: plaintext.length });
  return new Uint8Array(Buffer.concat([c.update(plaintext), c.final(), c.getAuthTag()]));
}
function aeadDecrypt(key: Uint8Array, nonce: Uint8Array, ct: Uint8Array, aad: Uint8Array): Uint8Array {
  if (ct.length < 16) throw new PrivacyCryptoError("ciphertext too short");
  const d = createDecipheriv("chacha20-poly1305", key, nonce, { authTagLength: 16 });
  d.setAAD(aad, { plaintextLength: ct.length - 16 });
  d.setAuthTag(Buffer.from(ct.subarray(ct.length - 16)));
  return new Uint8Array(Buffer.concat([d.update(ct.subarray(0, ct.length - 16)), d.final()]));
}

// --------------------------------------------------------------------------- keys, sealing
export function generateX25519Keypair(): { privateKeyHex: string; publicKeyHex: string } {
  const { privateKey } = generateKeyPairSync("x25519");
  const raw = new Uint8Array((privateKey.export({ format: "der", type: "pkcs8" }) as Buffer).subarray(-32));
  return { privateKeyHex: bytesToHex(raw), publicKeyHex: bytesToHex(pubRawOf(privObj(raw))) };
}

function sealKey(shared: Uint8Array, epk: Uint8Array, recipientPub: Uint8Array): Uint8Array {
  const info = Buffer.concat([Buffer.from(`${VERSION}|`), epk, recipientPub]);
  return new Uint8Array(hkdfSync("sha256", shared, new Uint8Array(0), info, 32));
}

export function seal(recipientPublicHex: string, plaintext: Uint8Array, aad: Uint8Array): SealedEnvelope {
  const rpub = hex32(recipientPublicHex, "recipient public key");
  const { privateKey } = generateKeyPairSync("x25519");
  const epk = pubRawOf(privateKey as ReturnType<typeof privObj>);
  const shared = new Uint8Array(diffieHellman({ privateKey, publicKey: pubObj(rpub) }));
  const nonce = new Uint8Array(randomBytes(12));
  const ct = aeadEncrypt(sealKey(shared, epk, rpub), nonce, plaintext, aad);
  return { v: VERSION, alg: SEAL_ALG, epk: bytesToHex(epk), nonce: bytesToHex(nonce), ct: bytesToHex(ct) };
}

export function openSealed(recipientPrivateHex: string, envelope: SealedEnvelope, aad: Uint8Array): Uint8Array {
  if (!envelope || envelope.v !== VERSION || envelope.alg !== SEAL_ALG) throw new PrivacyCryptoError("not a ccpp-v1 sealed envelope");
  try {
    const priv = privObj(hex32(recipientPrivateHex, "private key"));
    const epk = hex32(envelope.epk, "envelope epk");
    const shared = new Uint8Array(diffieHellman({ privateKey: priv, publicKey: pubObj(epk) }));
    return aeadDecrypt(sealKey(shared, epk, pubRawOf(priv)), hexToBytes(envelope.nonce), hexToBytes(envelope.ct), aad);
  } catch (e) {
    if (e instanceof PrivacyCryptoError) throw e;
    throw new PrivacyCryptoError("sealed envelope does not open with this key");
  }
}

export function encryptData(dataKey: Uint8Array, plaintext: Uint8Array, aad: Uint8Array): DataEnvelope {
  if (dataKey.length !== 32) throw new PrivacyCryptoError("data key must be 32 bytes");
  const nonce = new Uint8Array(randomBytes(12));
  return { v: VERSION, alg: DATA_ALG, nonce: bytesToHex(nonce), ct: bytesToHex(aeadEncrypt(dataKey, nonce, plaintext, aad)) };
}

export function decryptData(dataKey: Uint8Array, envelope: DataEnvelope, aad: Uint8Array): Uint8Array {
  if (!envelope || envelope.v !== VERSION || envelope.alg !== DATA_ALG) throw new PrivacyCryptoError("not a ccpp-v1 data envelope");
  try {
    return aeadDecrypt(dataKey, hexToBytes(envelope.nonce), hexToBytes(envelope.ct), aad);
  } catch {
    throw new PrivacyCryptoError("data envelope does not decrypt with this key");
  }
}

// --------------------------------------------------------------------------- Shamir over GF(256)
const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    EXP[i + 255] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
})();
const mul = (a: number, b: number): number => (a === 0 || b === 0 ? 0 : (EXP[(LOG[a] ?? 0) + (LOG[b] ?? 0)] ?? 0));
const div = (a: number, b: number): number => {
  if (b === 0) throw new PrivacyCryptoError("GF(256) division by zero");
  return a === 0 ? 0 : (EXP[(((LOG[a] ?? 0) - (LOG[b] ?? 0)) % 255 + 255) % 255] ?? 0);
};

export function splitSecret(secret: Uint8Array, n: number, k: number): Array<{ x: number; share: Uint8Array }> {
  if (!(k >= 1 && k <= n && n <= 255)) throw new PrivacyCryptoError("need 1 <= k <= n <= 255");
  const shares = Array.from({ length: n }, (_, i) => ({ x: i + 1, share: new Uint8Array(secret.length) }));
  for (let b = 0; b < secret.length; b++) {
    const coeffs = [secret[b] ?? 0, ...Array.from(randomBytes(k - 1))];
    for (const s of shares) {
      let acc = 0;
      let power = 1;
      for (const c of coeffs) {
        acc ^= mul(c, power);
        power = mul(power, s.x);
      }
      s.share[b] = acc;
    }
  }
  return shares;
}

export function reconstructSecret(shares: Array<{ x: number; share: Uint8Array }>, k: number): Uint8Array {
  if (shares.length < k) throw new PrivacyCryptoError(`need at least ${k} shares`);
  const use = shares.slice(0, k);
  if (new Set(use.map((s) => s.x)).size !== k) throw new PrivacyCryptoError("share x-coordinates must be distinct");
  const len = use[0]!.share.length;
  const w = use.map((sj, j) => {
    let num = 1;
    let den = 1;
    use.forEach((sm, m) => {
      if (m !== j) {
        num = mul(num, sm.x);
        den = mul(den, sj.x ^ sm.x);
      }
    });
    return div(num, den);
  });
  const out = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    let acc = 0;
    use.forEach((s, j) => {
      acc ^= mul(s.share[i] ?? 0, w[j] ?? 0);
    });
    out[i] = acc;
  }
  return out;
}

// --------------------------------------------------------------------------- guardian pinning, job preparation
export function guardianManifestFingerprint(guardians: Record<string, string>, threshold: number): string {
  const g: Record<string, string> = {};
  for (const [n, k] of Object.entries(guardians)) g[String(n)] = String(k).toLowerCase();
  return createHash("sha256").update(canonicalJson({ v: "ccpp-guardians-v1", threshold: Math.trunc(threshold), guardians: g })).digest("hex");
}

export function checkGuardianPin(
  guardians: Record<string, string>,
  threshold: number,
  pin: string | undefined,
  requirePin = false,
): void {
  if (!pin) {
    if (requirePin) throw new PrivacyCryptoError("no guardian fingerprint is pinned (requireGuardianPin is set)");
    return;
  }
  const actual = guardianManifestFingerprint(guardians, threshold);
  if (actual !== pin.trim().toLowerCase()) throw new PrivacyCryptoError(`guardian keys do not match the pinned fingerprint (got ${actual})`);
}

/** Encrypt `input` with a fresh Data Key, split the key across the guardians and seal each share to one guardian. */
export function preparePrivateInput(
  input: unknown,
  guardians: Record<string, string>,
  threshold: number,
  canonical = false,
): { encrypted_input: DataEnvelope; dk_shares: PrivatePart["dk_shares"] } {
  const names = Object.keys(guardians).sort();
  if (threshold < 2 || names.length < threshold) throw new PrivacyCryptoError("need at least `threshold` (>= 2) guardians");
  const dataKey = new Uint8Array(randomBytes(32));
  const encrypted_input = encryptData(dataKey, canonical ? canonicalJson(input) : enc.encode(JSON.stringify(input)), INPUT_AAD);
  const shares = splitSecret(dataKey, names.length, threshold);
  const dk_shares: PrivatePart["dk_shares"] = {};
  names.forEach((name, i) => {
    const s = shares[i]!;
    dk_shares[name] = { x: s.x, envelope: seal(guardians[name]!, s.share, shareAad(name)) };
  });
  return { encrypted_input, dk_shares };
}

// --------------------------------------------------------------------------- sealed stream chunks
/** Inverse of the agent's padding: `u32 big-endian length || text || zeros`. Rejects an over-long length and non-zero padding. */
export function unpadStreamPlaintext(data: Uint8Array): Uint8Array {
  if (data.length < 4) throw new PrivateStreamError("padded chunk is shorter than its 4 byte length prefix");
  const n = ((data[0]! << 24) | (data[1]! << 16) | (data[2]! << 8) | data[3]!) >>> 0;
  if (n > data.length - 4) throw new PrivateStreamError("padded chunk length exceeds its payload");
  for (let i = 4 + n; i < data.length; i++) {
    if (data[i] !== 0) throw new PrivateStreamError("padded chunk has non-zero padding bytes");
  }
  return data.subarray(4, 4 + n);
}

/** Frame like the agent (`pad_stream_plaintext`); here for tests and tools. `block <= 0` leaves the data raw. */
export function padStreamPlaintext(data: Uint8Array, block: number): Uint8Array {
  if (block <= 0) return data;
  const framedLen = 4 + data.length;
  const out = new Uint8Array(framedLen + ((block - (framedLen % block)) % block));
  new DataView(out.buffer).setUint32(0, data.length, false);
  out.set(data, 4);
  return out;
}

export function openStreamChunk(
  customerPrivateKey: string,
  envelope: SealedEnvelope,
  opts: { jobId: string; seq: number; final: boolean; padBytes?: number },
): string {
  let plain: Uint8Array;
  try {
    plain = openSealed(customerPrivateKey, envelope, streamAad(opts.jobId, opts.seq, opts.final));
  } catch (e) {
    throw new PrivateStreamError(`stream chunk ${opts.seq} does not open: ${(e as Error).message}`);
  }
  if ((opts.padBytes ?? DEFAULT_PAD_BYTES) > 0) plain = unpadStreamPlaintext(plain);
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(plain);
  } catch {
    throw new PrivateStreamError(`stream chunk ${opts.seq} is not valid UTF-8`);
  }
}

/** Stateful reader over the parsed SSE events of one private stream. */
export class PrivateStreamReader {
  jobId: string | undefined;
  nextSeq = 0;
  finished = false;
  constructor(
    private readonly customerPrivateKey: string,
    private readonly padBytes: number = DEFAULT_PAD_BYTES,
    jobId?: string,
  ) {
    this.jobId = jobId;
  }

  feed(event: unknown): string {
    if (event === null || typeof event !== "object") throw new PrivateStreamError("stream event is not an object");
    const e = event as Record<string, unknown>;
    if (e["error"] && typeof e["error"] === "object") {
      const er = e["error"] as Record<string, unknown>;
      throw new PrivateStreamError(`stream error: ${String(er["code"] ?? er["message"] ?? "unknown")}`);
    }
    if (e["object"] !== "chat.completion.chunk.sealed") throw new PrivateStreamError("unexpected (possibly plaintext) event in a private stream");
    if (this.finished) throw new PrivateStreamError("chunk after the final chunk");
    const { job_id: jobId, seq, final, sealed } = e as { job_id?: unknown; seq?: unknown; final?: unknown; sealed?: unknown };
    if (typeof jobId !== "string" || !jobId) throw new PrivateStreamError("sealed chunk has no job_id");
    if (this.jobId === undefined) this.jobId = jobId;
    else if (jobId !== this.jobId) throw new PrivateStreamError("sealed chunk belongs to another job");
    if (typeof seq !== "number" || seq !== this.nextSeq) throw new PrivateStreamError(`expected chunk ${this.nextSeq}, got ${String(seq)}`);
    if (typeof final !== "boolean") throw new PrivateStreamError("sealed chunk has no final flag");
    const text = openStreamChunk(this.customerPrivateKey, sealed as SealedEnvelope, { jobId, seq, final, padBytes: this.padBytes });
    this.nextSeq += 1;
    this.finished = final;
    return text;
  }

  close(): void {
    if (!this.finished) throw new PrivateStreamError("sealed stream ended without its final chunk (truncated)");
  }
}

/** Parse an SSE byte stream into the JSON payloads of its `data:` lines; `[DONE]` ends it. */
export async function* iterSseData(body: ReadableStream<Uint8Array> | AsyncIterable<Uint8Array>): AsyncGenerator<unknown> {
  const decoder = new TextDecoder();
  let buf = "";
  const chunks: AsyncIterable<Uint8Array> =
    Symbol.asyncIterator in (body as object)
      ? (body as AsyncIterable<Uint8Array>)
      : (async function* () {
          const reader = (body as ReadableStream<Uint8Array>).getReader();
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) return;
              if (value) yield value;
            }
          } finally {
            reader.releaseLock();
          }
        })();
  const handle = function* (line: string): Generator<unknown, boolean> {
    const l = line.trim();
    if (!l.startsWith("data:")) return false;
    const payload = l.slice(5).trim();
    if (payload === "[DONE]") return true;
    if (payload) {
      try {
        yield JSON.parse(payload);
      } catch {
        throw new PrivateStreamError("stream event is not JSON");
      }
    }
    return false;
  };
  for await (const c of chunks) {
    buf += decoder.decode(c, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (yield* handle(line)) return;
    }
  }
  buf += decoder.decode();
  if (buf) yield* handle(buf);
}

/** Decrypted text deltas from the SSE body of a private chat stream. Throws if the stream is truncated. */
export async function* iterPrivateText(
  body: ReadableStream<Uint8Array> | AsyncIterable<Uint8Array>,
  customerPrivateKey: string,
  padBytes: number = DEFAULT_PAD_BYTES,
): AsyncGenerator<string> {
  const reader = new PrivateStreamReader(customerPrivateKey, padBytes);
  for await (const ev of iterSseData(body)) yield reader.feed(ev);
  reader.close();
}

// --------------------------------------------------------------------------- private Kit input
export type KitInputValue = Uint8Array | string | { b64: string } | { text: string };
export interface PrivateKitInput {
  inputs: Record<string, { b64: string } | { text: string }>;
  params: Record<string, unknown>;
}
const NAME = /^[A-Za-z0-9._-]{1,128}$/;

/** Decrypted input of a private chat job: `{prompt, messages, max_tokens?, temperature?}` (same as the Python SDK). */
export interface PrivateChatInput {
  prompt: string;
  messages: Array<{ role: string; content: string }>;
  max_tokens?: number;
  temperature?: number;
}

export function buildPrivateChatInput(
  messages: Array<{ role: string; content: string }>,
  opts: { max_tokens?: number | undefined; temperature?: number | undefined } = {},
): PrivateChatInput {
  if (!messages || messages.length === 0) throw new Error("a chat needs at least one message");
  const msgs = messages.map((m) => ({ role: String(m.role), content: String(m.content) }));
  const out: PrivateChatInput = { prompt: msgs.map((m) => `${m.role}: ${m.content}`).join("\n"), messages: msgs };
  if (opts.max_tokens !== undefined && opts.max_tokens !== null) {
    if (!Number.isInteger(opts.max_tokens) || opts.max_tokens < 1) throw new Error("max_tokens must be a positive integer");
    out.max_tokens = opts.max_tokens;
  }
  if (opts.temperature !== undefined && opts.temperature !== null) {
    if (typeof opts.temperature !== "number" || !(opts.temperature >= 0 && opts.temperature <= 2)) throw new Error("temperature must be a number between 0 and 2");
    out.temperature = opts.temperature;
  }
  return out;
}

/** Plaintext of one private Kit part: `{"inputs": {name: {"b64"} | {"text"}}, "params": {...}}` (spec ccpp-v1.md). */
export function buildPrivateKitInput(
  inputs: Record<string, KitInputValue> = {},
  params: Record<string, unknown> = {},
): PrivateKitInput {
  const out: PrivateKitInput["inputs"] = {};
  for (const [name, v] of Object.entries(inputs)) {
    if (!NAME.test(name)) throw new Error(`bad input name "${name}": use 1-128 of A-Z a-z 0-9 . _ -`);
    if (v instanceof Uint8Array) out[name] = { b64: Buffer.from(v).toString("base64") };
    else if (typeof v === "string") out[name] = { text: v };
    else if (v && typeof v === "object" && Object.keys(v).length === 1 && typeof (v as { b64?: unknown }).b64 === "string") {
      const b64 = (v as { b64: string }).b64;
      if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(b64)) throw new Error(`input "${name}": b64 is not valid base64`);
      out[name] = { b64 };
    } else if (v && typeof v === "object" && Object.keys(v).length === 1 && typeof (v as { text?: unknown }).text === "string") {
      out[name] = { text: (v as { text: string }).text };
    } else throw new Error(`input "${name}" must be bytes, string, {b64} or {text}`);
  }
  return { inputs: out, params: { ...params } };
}

/** One `private_parts` entry per plaintext, with a fresh Data Key, guardian shares and result keypair per part. */
export function preparePrivateFanoutParts(
  plaintexts: PrivateKitInput[],
  guardians: Record<string, string>,
  threshold: number,
): { parts: PrivatePart[]; resultPrivateKeys: string[] } {
  if (plaintexts.length === 0) throw new Error("a private fan-out needs at least one part");
  const parts: PrivatePart[] = [];
  const resultPrivateKeys: string[] = [];
  for (const p of plaintexts) {
    if (!p || typeof p !== "object" || Object.keys(p).some((k) => k !== "inputs" && k !== "params")) {
      throw new Error("each part plaintext must be {inputs, params} (see buildPrivateKitInput)");
    }
    const kp = generateX25519Keypair();
    parts.push({ ...preparePrivateInput(p, guardians, threshold), result_public_key: kp.publicKeyHex });
    resultPrivateKeys.push(kp.privateKeyHex);
  }
  return { parts, resultPrivateKeys };
}
