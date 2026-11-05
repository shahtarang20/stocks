/** Main-thread side of the hash worker, with a chunked fallback when Workers are unavailable. */
import { sha256 } from "@noble/hashes/sha2.js";

type Msg = { id: number; type: "progress" | "done" | "error"; hex?: string; done?: number; total?: number; message?: string };

let worker: Worker | null = null;
let seq = 0;
const pending = new Map<number, { resolve: (h: string) => void; reject: (e: Error) => void; onProgress?: (d: number, t: number) => void }>();

function getWorker(): Worker | null {
  if (typeof Worker === "undefined") return null;
  if (!worker) {
    worker = new Worker(new URL("./hash.worker.ts", import.meta.url), { type: "module" });
    worker.onmessage = (e: MessageEvent<Msg>) => {
      const p = pending.get(e.data.id);
      if (!p) return;
      if (e.data.type === "progress") p.onProgress?.(e.data.done ?? 0, e.data.total ?? 0);
      else {
        pending.delete(e.data.id);
        if (e.data.type === "done") p.resolve(e.data.hex as string);
        else p.reject(new Error(e.data.message ?? "hash failed"));
      }
    };
    worker.onerror = () => {
      for (const p of pending.values()) p.reject(new Error("hash worker crashed"));
      pending.clear();
      worker = null;
    };
  }
  return worker;
}

const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

function viaWorker(kind: "file" | "blob", blob: Blob, onProgress?: (d: number, t: number) => void): Promise<string> | null {
  const w = getWorker();
  if (!w) return null;
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject, onProgress });
    w.postMessage({ id, kind, blob });
  });
}

/** Whole-file SHA-256 (hex), streamed. */
export async function hashFile(file: Blob, onProgress?: (done: number, total: number) => void): Promise<string> {
  const w = viaWorker("file", file, onProgress);
  if (w) return w;
  const h = sha256.create();
  const SLICE = 4 * 1024 * 1024;
  for (let off = 0; off < file.size; off += SLICE) {
    h.update(new Uint8Array(await file.slice(off, off + SLICE).arrayBuffer()));
    onProgress?.(Math.min(off + SLICE, file.size), file.size);
    await new Promise((r) => setTimeout(r, 0)); // let the UI breathe on the fallback path
  }
  return hex(h.digest());
}

/** SHA-256 (hex) of one part with SubtleCrypto. */
export async function hashPart(part: Blob): Promise<string> {
  const w = viaWorker("blob", part);
  if (w) return w;
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", await part.arrayBuffer())));
}
