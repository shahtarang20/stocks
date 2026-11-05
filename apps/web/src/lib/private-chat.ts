/**
 * Private (sealed) chat streaming for the browser (U18, spec ccpp-v1.md 6g).
 *
 * The messages are encrypted here, the API relays only ciphertext, and every chunk is opened with the customer's result
 * private key and un-padded here. A non-private chat stream is not handled by this module and stays unchanged.
 * `fetchImpl` is injectable for tests.
 */

import { API_BASE_URL, getStoredAccessToken } from "./api";
import {
  DEFAULT_STREAM_PAD_BYTES,
  PrivateStreamReader,
  generateX25519Keypair,
  preparePrivateJob,
} from "./ccpp";

export interface PrivateChatOptions {
  model: string;
  messages: Array<{ role: string; content: string }>;
  isolationTier?: "sealed" | "sealed_pro" | "confidential";
  guardians: Record<string, string>;
  threshold: number;
  /** Must equal the platform's PRIVACY_STREAM_PAD_BYTES (default 256; 0 = unpadded). */
  padBytes?: number;
  /** Result keypair; a fresh one is made per call when omitted (the stream is consumed immediately). */
  resultKey?: { privateKeyHex: string; publicKeyHex: string };
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
  token?: string | null;
}

/** Parse the `data:` lines of an SSE byte stream into JSON values. `[DONE]` ends it. */
export async function* sseJson(body: ReadableStream<Uint8Array>): AsyncGenerator<unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const parse = (line: string): { done: boolean; value?: unknown } => {
    const l = line.trim();
    if (!l.startsWith("data:")) return { done: false };
    const payload = l.slice(5).trim();
    if (payload === "[DONE]") return { done: true };
    if (!payload) return { done: false };
    return { done: false, value: JSON.parse(payload) };
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let i: number;
      while ((i = buf.indexOf("\n")) >= 0) {
        const r = parse(buf.slice(0, i));
        buf = buf.slice(i + 1);
        if (r.done) return;
        if (r.value !== undefined) yield r.value;
      }
    }
    buf += decoder.decode();
    if (buf) {
      const r = parse(buf);
      if (!r.done && r.value !== undefined) yield r.value;
    }
  } finally {
    reader.releaseLock();
  }
}

/** Decrypted text deltas of a private chat. Throws PrivateStreamError on a truncated or tampered stream. */
export async function* streamPrivateChat(opts: PrivateChatOptions): AsyncGenerator<string> {
  const key = opts.resultKey ?? generateX25519Keypair();
  const messages = opts.messages.map((m) => ({ role: m.role, content: m.content }));
  const prepared = preparePrivateJob(
    { prompt: messages.map((m) => `${m.role}: ${m.content}`).join("\n"), messages },
    opts.guardians,
    opts.threshold,
    key.publicKeyHex
  );
  const token = opts.token === undefined ? getStoredAccessToken() : opts.token;
  const doFetch = opts.fetchImpl ?? fetch;
  const res = await doFetch(`${opts.baseUrl ?? API_BASE_URL}/v1/chat/completions`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      model: opts.model,
      messages: [{ role: "user", content: "sealed" }], // placeholder; the real messages are in encrypted_input
      stream: true,
      isolation_tier: opts.isolationTier ?? "sealed",
      encrypted_input: prepared.encrypted_input,
      dk_shares: prepared.dk_shares,
      result_public_key: key.publicKeyHex,
    }),
    ...(opts.signal ? { signal: opts.signal } : {}),
  });
  if (!res.ok || !res.body) {
    // e.g. 400 streaming_not_supported_for_private when the server flag is off
    let code = `http_${res.status}`;
    try {
      const j = (await res.json()) as { detail?: unknown };
      code = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* keep the status code */
    }
    throw new Error(code);
  }
  const reader = new PrivateStreamReader(key.privateKeyHex, opts.padBytes ?? DEFAULT_STREAM_PAD_BYTES);
  for await (const ev of sseJson(res.body)) yield reader.feed(ev);
  reader.close();
}
