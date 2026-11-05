/// Hash worker (U17): keeps SHA-256 of large files off the UI thread.
///  - "file": whole-file SHA-256, streamed in 4 MiB slices with @noble/hashes (SubtleCrypto has no incremental API,
///    and a 50 GB file cannot be held in memory).
///  - "blob": one upload part, hashed in one go with SubtleCrypto (a part is at most a few hundred MB).
import { sha256 } from "@noble/hashes/sha2.js";

type In = { id: number; kind: "file" | "blob"; blob: Blob };
const ctx = self as unknown as {
  postMessage: (m: unknown) => void;
  onmessage: ((e: MessageEvent<In>) => void) | null;
};

const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const SLICE = 4 * 1024 * 1024;

ctx.onmessage = async (e) => {
  const { id, kind, blob } = e.data;
  try {
    if (kind === "blob") {
      const d = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
      ctx.postMessage({ id, type: "done", hex: hex(new Uint8Array(d)) });
      return;
    }
    const h = sha256.create();
    for (let off = 0; off < blob.size; off += SLICE) {
      h.update(new Uint8Array(await blob.slice(off, off + SLICE).arrayBuffer()));
      ctx.postMessage({ id, type: "progress", done: Math.min(off + SLICE, blob.size), total: blob.size });
    }
    ctx.postMessage({ id, type: "done", hex: hex(h.digest()) });
  } catch (err) {
    ctx.postMessage({ id, type: "error", message: err instanceof Error ? err.message : String(err) });
  }
};
