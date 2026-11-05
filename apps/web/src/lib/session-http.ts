/**
 * U10 HTTP-over-encrypted-channel proxy (browser side). Mirrors provider-agent/computecoin_agent/session_http.py and
 * docs/architecture/U10-interactive-sessions-design.md section 9. Pure logic: the caller supplies `sendFrame`
 * (which seals the payload as plaintext type T_HTTP) and feeds decrypted T_HTTP payloads to `onFrame`.
 */

export const K = { REQ_HEAD: 0, REQ_BODY: 1, REQ_END: 2, RES_HEAD: 3, RES_BODY: 4, RES_END: 5, ERR: 6, WS_OPEN: 7, WS_DATA: 8, WS_CLOSE: 9, CANCEL: 10 } as const;
export const CHUNK = 64 * 1024;
export const MAX_REQ_BODY = 8 * 1024 * 1024;

const te = new TextEncoder();
const td = new TextDecoder();

export function packFrame(kind: number, id: number, data: Uint8Array = new Uint8Array(0)): Uint8Array {
  const out = new Uint8Array(5 + data.length);
  out[0] = kind;
  new DataView(out.buffer).setUint32(1, id, false);
  out.set(data, 5);
  return out;
}

export interface HttpSink {
  head(status: number, headers: [string, string][]): void;
  body(chunk: Uint8Array): void;
  end(): void;
  error(code: string): void;
}

export interface WsHooks {
  open(protocol: string): void;
  message(data: string | ArrayBuffer): void;
  close(code: number, reason: string): void;
  error(): void;
}

export interface WsHandle {
  sendText(s: string): void;
  sendBinary(b: ArrayBuffer | ArrayBufferView): void;
  close(code?: number, reason?: string): void;
}

/** Minimal RFC 6455 codec for the tunnel: client frames are masked, server frames are not. */
export class WsCodec {
  private buf = new Uint8Array(0);
  private frag: { op: number; parts: Uint8Array[] } | null = null;
  static MAX_MESSAGE = 16 * 1024 * 1024;

  encode(op: number, payload: Uint8Array): Uint8Array {
    const n = payload.length;
    const head = n < 126 ? 2 : n < 65536 ? 4 : 10;
    const out = new Uint8Array(head + 4 + n);
    out[0] = 0x80 | op;
    if (n < 126) out[1] = 0x80 | n;
    else if (n < 65536) { out[1] = 0x80 | 126; new DataView(out.buffer).setUint16(2, n, false); }
    else { out[1] = 0x80 | 127; new DataView(out.buffer).setBigUint64(2, BigInt(n), false); }
    const mask = crypto.getRandomValues(new Uint8Array(4));
    out.set(mask, head);
    for (let i = 0; i < n; i++) out[head + 4 + i] = payload[i] ^ mask[i & 3];
    return out;
  }

  /** Feed bytes; returns completed events. Throws on a protocol violation (the caller closes the tunnel). */
  feed(data: Uint8Array): Array<{ op: number; payload: Uint8Array }> {
    const merged = new Uint8Array(this.buf.length + data.length);
    merged.set(this.buf, 0);
    merged.set(data, this.buf.length);
    this.buf = merged;
    const out: Array<{ op: number; payload: Uint8Array }> = [];
    for (;;) {
      const b = this.buf;
      if (b.length < 2) break;
      const fin = (b[0] & 0x80) !== 0;
      const op = b[0] & 0x0f;
      if (b[0] & 0x70) throw new Error("reserved bits");
      if (b[1] & 0x80) throw new Error("server frame must not be masked");
      let len = b[1] & 0x7f;
      let off = 2;
      if (len === 126) { if (b.length < 4) break; len = new DataView(b.buffer, b.byteOffset).getUint16(2, false); off = 4; }
      else if (len === 127) {
        if (b.length < 10) break;
        const big = new DataView(b.buffer, b.byteOffset).getBigUint64(2, false);
        if (big > BigInt(WsCodec.MAX_MESSAGE)) throw new Error("frame too large");
        len = Number(big); off = 10;
      }
      if (len > WsCodec.MAX_MESSAGE) throw new Error("frame too large");
      if (b.length < off + len) break;
      const payload = b.slice(off, off + len);
      this.buf = b.slice(off + len);
      if (op >= 8) { if (!fin || len > 125) throw new Error("bad control frame"); out.push({ op, payload }); continue; }
      if (op === 0) {
        if (!this.frag) throw new Error("unexpected continuation");
        this.frag.parts.push(payload);
      } else {
        if (this.frag) throw new Error("interleaved data frames");
        this.frag = { op, parts: [payload] };
      }
      if (fin) {
        const f = this.frag!;
        this.frag = null;
        const total = f.parts.reduce((n, p) => n + p.length, 0);
        if (total > WsCodec.MAX_MESSAGE) throw new Error("message too large");
        const msg = new Uint8Array(total);
        let o = 0;
        for (const p of f.parts) { msg.set(p, o); o += p.length; }
        out.push({ op: f.op, payload: msg });
      }
    }
    return out;
  }
}

interface Pending {
  sink?: HttpSink;
  ws?: { hooks: WsHooks; codec: WsCodec; opened: boolean; closed: boolean };
}

export class SessionHttpProxy {
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private jar = new Map<string, string>(); // in-memory cookie jar: a synthesized Response cannot set cookies

  constructor(private readonly sendFrame: (payload: Uint8Array) => void) {}

  private cookieHeader(): string | null {
    return this.jar.size ? Array.from(this.jar, ([k, v]) => `${k}=${v}`).join("; ") : null;
  }

  private absorbCookies(headers: [string, string][]): [string, string][] {
    const rest: [string, string][] = [];
    for (const [k, v] of headers) {
      if (k !== "set-cookie") { rest.push([k, v]); continue; }
      const parts = v.split(";").map((x) => x.trim());
      const eq = parts[0].indexOf("=");
      if (eq <= 0) continue;
      const name = parts[0].slice(0, eq);
      const value = parts[0].slice(eq + 1);
      const maxAge = parts.find((p) => /^max-age=/i.test(p));
      const expires = parts.find((p) => /^expires=/i.test(p));
      const dead = (maxAge && parseInt(maxAge.slice(8), 10) <= 0) || (expires && Date.parse(expires.slice(8)) < Date.now()) || value === "";
      if (dead) this.jar.delete(name); else if (this.jar.size < 64) this.jar.set(name, value.slice(0, 4096));
    }
    return rest;
  }

  /** Send one HTTP request. Returns a cancel function. */
  request(req: { method: string; path: string; headers: [string, string][]; body?: Uint8Array }, sink: HttpSink): () => void {
    if ((req.body?.length ?? 0) > MAX_REQ_BODY) { queueMicrotask(() => sink.error("request_too_large")); return () => {}; }
    const id = this.nextId++ >>> 0;
    this.pending.set(id, { sink });
    const headers = req.headers.filter(([k]) => k.toLowerCase() !== "cookie");
    const c = this.cookieHeader();
    if (c) headers.push(["cookie", c]);
    this.sendFrame(packFrame(K.REQ_HEAD, id, te.encode(JSON.stringify({ method: req.method, path: req.path, headers }))));
    const body = req.body ?? new Uint8Array(0);
    for (let i = 0; i < body.length; i += CHUNK) this.sendFrame(packFrame(K.REQ_BODY, id, body.subarray(i, i + CHUNK)));
    this.sendFrame(packFrame(K.REQ_END, id));
    return () => { if (this.pending.delete(id)) this.sendFrame(packFrame(K.CANCEL, id)); };
  }

  openWebSocket(path: string, protocols: string[], hooks: WsHooks): WsHandle {
    const id = this.nextId++ >>> 0;
    const codec = new WsCodec();
    const st = { hooks, codec, opened: false, closed: false };
    this.pending.set(id, { ws: st });
    const headers: [string, string][] = protocols.length ? [["sec-websocket-protocol", protocols.join(", ")]] : [];
    const c = this.cookieHeader();
    if (c) headers.push(["cookie", c]);
    this.sendFrame(packFrame(K.WS_OPEN, id, te.encode(JSON.stringify({ path, headers }))));
    const raw = (op: number, p: Uint8Array) => { if (st.opened && !st.closed) this.sendFrame(packFrame(K.WS_DATA, id, codec.encode(op, p))); };
    const finish = (code: number, reason: string) => {
      if (st.closed) return;
      st.closed = true;
      this.pending.delete(id);
      this.sendFrame(packFrame(K.WS_CLOSE, id));
      hooks.close(code, reason);
    };
    return {
      sendText: (s) => raw(1, te.encode(s)),
      sendBinary: (b) => raw(2, b instanceof ArrayBuffer ? new Uint8Array(b) : new Uint8Array(b.buffer, b.byteOffset, b.byteLength)),
      close: (code = 1000, reason = "") => {
        if (st.opened && !st.closed) {
          const r = te.encode(reason).slice(0, 123);
          const p = new Uint8Array(2 + r.length);
          new DataView(p.buffer).setUint16(0, code, false);
          p.set(r, 2);
          this.sendFrame(packFrame(K.WS_DATA, id, codec.encode(8, p)));
        }
        finish(code, reason);
      },
    };
  }

  /** Feed one decrypted T_HTTP payload. Never throws on peer garbage. */
  onFrame(payload: Uint8Array): void {
    if (payload.length < 5) return;
    const kind = payload[0];
    const id = new DataView(payload.buffer, payload.byteOffset).getUint32(1, false);
    const data = payload.subarray(5);
    const p = this.pending.get(id);
    if (!p) return;
    try {
      if (p.sink) {
        if (kind === K.RES_HEAD) {
          const h = JSON.parse(td.decode(data)) as { status: number; headers: [string, string][] };
          p.sink.head(h.status, this.absorbCookies(h.headers));
        } else if (kind === K.RES_BODY) p.sink.body(data.slice());
        else if (kind === K.RES_END) { this.pending.delete(id); p.sink.end(); }
        else if (kind === K.ERR) { this.pending.delete(id); p.sink.error(JSON.parse(td.decode(data)).code ?? "error"); }
        return;
      }
      const w = p.ws!;
      if (kind === K.RES_HEAD) {
        const h = JSON.parse(td.decode(data)) as { status: number; headers: [string, string][] };
        if (h.status === 101) { w.opened = true; w.hooks.open(h.headers.find(([k]) => k === "sec-websocket-protocol")?.[1] ?? ""); }
        else { this.pending.delete(id); w.closed = true; w.hooks.error(); w.hooks.close(1006, "upgrade refused"); }
      } else if (kind === K.WS_DATA && w.opened) {
        for (const ev of w.codec.feed(data)) {
          if (ev.op === 1) w.hooks.message(td.decode(ev.payload));
          else if (ev.op === 2) w.hooks.message(ev.payload.buffer.slice(ev.payload.byteOffset, ev.payload.byteOffset + ev.payload.byteLength) as ArrayBuffer);
          else if (ev.op === 9) this.sendFrame(packFrame(K.WS_DATA, id, w.codec.encode(10, ev.payload)));
          else if (ev.op === 8) {
            const code = ev.payload.length >= 2 ? new DataView(ev.payload.buffer, ev.payload.byteOffset).getUint16(0, false) : 1005;
            w.closed = true; this.pending.delete(id); this.sendFrame(packFrame(K.WS_CLOSE, id));
            w.hooks.close(code, td.decode(ev.payload.slice(2)));
            return;
          }
        }
      } else if (kind === K.WS_CLOSE || kind === K.ERR) {
        this.pending.delete(id);
        if (!w.closed) { w.closed = true; if (kind === K.ERR) w.hooks.error(); w.hooks.close(1006, ""); }
      }
    } catch {
      this.pending.delete(id);
      if (p.sink) p.sink.error("bad_frame");
      else if (p.ws && !p.ws.closed) { p.ws.closed = true; this.sendFrame(packFrame(K.WS_CLOSE, id)); p.ws.hooks.error(); p.ws.hooks.close(1002, "protocol error"); }
    }
  }

  /** The channel went away: fail everything in flight. */
  failAll(): void {
    for (const [id, p] of Array.from(this.pending)) {
      this.pending.delete(id);
      if (p.sink) p.sink.error("channel_closed");
      else if (p.ws && !p.ws.closed) { p.ws.closed = true; p.ws.hooks.close(1006, "channel closed"); }
    }
  }
}
