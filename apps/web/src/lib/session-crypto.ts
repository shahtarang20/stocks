/**
 * U10 end-to-end session crypto (browser side). Mirrors provider-agent/computecoin_agent/session_agent.py.
 * The relay never sees a private key or a derived key; it only forwards the two public keys and ciphertext.
 */
import { x25519 } from "@noble/curves/ed25519.js";
import { chacha20poly1305 } from "@noble/ciphers/chacha.js";
import { hkdf } from "@noble/hashes/hkdf.js";
import { sha256 } from "@noble/hashes/sha2.js";

export const T_DATA = 0;
export const T_RESIZE = 1;
export const T_CLOSE = 2;
export const T_READY = 3;
export const T_HTTP = 4; // HTTP-over-channel proxy frames (lib/session-http.ts)

const enc = new TextEncoder();
const INFO = enc.encode("ccsess-v1");

export function toHex(b: Uint8Array): string {
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
export function fromHex(h: string): Uint8Array {
  if (!/^[0-9a-f]{64}$/.test(h)) throw new Error("bad key");
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i++) out[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16);
  return out;
}
function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}
function nonce(n: number): Uint8Array {
  const out = new Uint8Array(12);
  new DataView(out.buffer).setBigUint64(4, BigInt(n), false);
  return out;
}

/** The browser's ephemeral keypair, created BEFORE the session so its public half goes in the create call. */
export function newClientKey(): { priv: Uint8Array; pubHex: string } {
  const priv = x25519.utils.randomSecretKey();
  return { priv, pubHex: toHex(x25519.getPublicKey(priv)) };
}

/** The private key stays in this tab's sessionStorage (never sent anywhere); the public half goes to the API. */
export function storeClientKey(id: string, priv: Uint8Array) {
  try {
    sessionStorage.setItem(`ccsess:${id}`, Array.from(priv, (b) => b.toString(16).padStart(2, "0")).join(""));
  } catch { /* private window: the session then cannot be reopened after a reload */ }
}

export class SessionChannel {
  private txN = 0;
  private rxN = 0;
  private readonly aad: Uint8Array;
  private readonly kB2A: Uint8Array;
  private readonly kA2B: Uint8Array;

  constructor(sessionId: string, clientPriv: Uint8Array, hostStaticPubHex: string, hostEphemeralPubHex: string) {
    const sa = fromHex(hostStaticPubHex);
    const ea = fromHex(hostEphemeralPubHex);
    const eb = x25519.getPublicKey(clientPriv);
    const ikm = concat(x25519.getSharedSecret(clientPriv, sa), x25519.getSharedSecret(clientPriv, ea));
    const okm = hkdf(sha256, ikm, sha256(enc.encode(sessionId)), concat(INFO, eb, sa, ea), 64);
    this.kB2A = okm.slice(0, 32);
    this.kA2B = okm.slice(32);
    this.aad = enc.encode(sessionId);
  }

  seal(kind: number, payload: Uint8Array): Uint8Array {
    const c = chacha20poly1305(this.kB2A, nonce(this.txN), concat(this.aad, new Uint8Array([0])));
    const out = c.encrypt(concat(new Uint8Array([kind]), payload));
    this.txN++;
    return out;
  }

  open(frame: Uint8Array): { kind: number; payload: Uint8Array } {
    const c = chacha20poly1305(this.kA2B, nonce(this.rxN), concat(this.aad, new Uint8Array([1])));
    const plain = c.decrypt(frame); // throws on tamper, drop, replay or reorder
    this.rxN++;
    return { kind: plain[0], payload: plain.slice(1) };
  }
}
