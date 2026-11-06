/**
 * Resumable multipart upload straight to storage (U17 over the U2 data plane).
 *
 * 1. Hash the whole file in a Web Worker (the server needs it up front; same file twice is stored once).
 * 2. POST /v1/data/uploads with a stable Idempotency-Key, so re-selecting the same file resumes the same upload.
 * 3. For each part not yet proven: hash it (SubtleCrypto), get a presigned PUT, PUT the bytes to storage with
 *    progress, then confirm. A 409 {"action":"resend"} from confirm re-sends that part. Each part retries with
 *    backoff. Pause aborts in-flight PUTs; resume re-reads server state and carries on.
 * 4. POST .../complete. The server proves the whole-file hash before the file is usable.
 */

import { ApiError } from "@/lib/api";
import { dataApi, type DataFile, type DataUpload } from "@/lib/kits";
import { hashFile, hashPart } from "@/lib/hashing";

export type UploadPhase = "hashing" | "starting" | "uploading" | "paused" | "completing" | "done" | "error" | "cancelled";

export interface UploadProgress {
  phase: UploadPhase;
  /** 0..1 of the current phase's work (hashing, then bytes uploaded). */
  fraction: number;
  bytesUploaded: number;
  totalBytes: number;
  partsDone: number;
  partCount: number;
  retries: number;
  file?: DataFile;
  deduplicated?: boolean;
  error?: string;
}

export interface UploaderOptions {
  token: string;
  file: File;
  concurrency?: number;
  maxPartAttempts?: number;
  onProgress: (p: UploadProgress) => void;
}

export interface Uploader {
  start(): Promise<void>;
  pause(): void;
  resume(): Promise<void>;
  cancel(): Promise<void>;
}

class Paused extends Error {}

export function uploadIdempotencyKey(sha256: string, size: number): string {
  return `up-${sha256.slice(0, 40)}-${size}`;
}

/**
 * The name sent to the server: the last path segment only. Browsers already give a bare file name, on Windows too, but a
 * drag from a zip, a pasted "C:\dir\file.bin" or a relative path would otherwise be refused by the server (it rejects
 * both "/" and a backslash). Both separators are handled whatever OS the browser runs on.
 */
export function uploadFileName(rawName: string): string {
  const base = rawName.split(/[\\/]/).pop() ?? "";
  return base.trim() || "upload.bin";
}

export function backoffMs(attempt: number): number {
  return Math.min(8000, 500 * 2 ** attempt);
}

/** PUT one part to its presigned URL with byte progress; abortable. */
function putPart(
  url: string, headers: Record<string, string>, body: Blob, signal: AbortSignal, onBytes: (n: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => onBytes(e.loaded);
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`storage returned ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("network error while uploading"));
    xhr.onabort = () => reject(new Paused());
    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(body);
  });
}

export function createUploader(opts: UploaderOptions): Uploader {
  const { token, file } = opts;
  const concurrency = opts.concurrency ?? 3;
  const maxAttempts = opts.maxPartAttempts ?? 4;

  let sha: string | null = null;
  let upload: DataUpload | null = null;
  let ctl = new AbortController();
  let cancelled = false;
  let retries = 0;
  const verified = new Set<number>();
  const inflight = new Map<number, number>();

  const state: UploadProgress = {
    phase: "hashing", fraction: 0, bytesUploaded: 0, totalBytes: file.size, partsDone: 0, partCount: 0, retries: 0,
  };
  const emit = (patch: Partial<UploadProgress>) => {
    Object.assign(state, patch, { retries });
    opts.onProgress({ ...state });
  };

  const partSize = (n: number) => {
    const u = upload as DataUpload;
    return n < u.part_count ? u.part_size : u.size_bytes - u.part_size * (u.part_count - 1);
  };
  const report = () => {
    const u = upload as DataUpload;
    let bytes = 0;
    for (const n of verified) bytes += partSize(n);
    for (const [n, loaded] of inflight) if (!verified.has(n)) bytes += Math.min(loaded, partSize(n));
    emit({ phase: "uploading", bytesUploaded: bytes, fraction: bytes / u.size_bytes, partsDone: verified.size });
  };

  async function sendPart(n: number): Promise<void> {
    const u = upload as DataUpload;
    const start = (n - 1) * u.part_size;
    const blob = file.slice(start, start + partSize(n));
    let lastErr: unknown;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      if (ctl.signal.aborted) throw new Paused();
      try {
        const digest = await hashPart(blob);
        const pu = await dataApi.partUrl(token, u.id, n, digest, blob.size);
        if (!pu.already_uploaded) {
          if (!pu.url) throw new Error("no upload URL for this part");
          await putPart(pu.url, pu.headers ?? {}, blob, ctl.signal, (loaded) => {
            inflight.set(n, loaded);
            report();
          });
        }
        try {
          await dataApi.confirmPart(token, u.id, n);
        } catch (err) {
          if (err instanceof ApiError && err.status === 409) throw new Error("part was corrupt in storage; re-sending");
          throw err;
        }
        verified.add(n);
        inflight.delete(n);
        report();
        return;
      } catch (err) {
        if (err instanceof Paused || ctl.signal.aborted) throw new Paused();
        lastErr = err;
        inflight.delete(n);
        retries += 1;
        report();
        await new Promise((r) => setTimeout(r, backoffMs(attempt)));
      }
    }
    throw lastErr instanceof Error ? lastErr : new Error(`part ${n} failed`);
  }

  async function runParts(): Promise<void> {
    const u = upload as DataUpload;
    const st = await dataApi.getUpload(token, u.id);
    verified.clear();
    st.verified_parts.forEach((n) => verified.add(n));
    report();
    const queue: number[] = [];
    for (let n = 1; n <= u.part_count; n++) if (!verified.has(n)) queue.push(n);
    let failure: unknown = null;
    const lane = async () => {
      while (queue.length && !failure && !ctl.signal.aborted) {
        const n = queue.shift() as number;
        try {
          await sendPart(n);
        } catch (err) {
          if (!(err instanceof Paused)) failure = err;
          return;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, queue.length) }, lane));
    if (failure) throw failure;
    if (ctl.signal.aborted) throw new Paused();
  }

  async function finish(): Promise<void> {
    emit({ phase: "completing", fraction: 1 });
    const done = await dataApi.completeUpload(token, (upload as DataUpload).id);
    if (!done.file) throw new Error("The upload finished but the file was not accepted.");
    emit({ phase: "done", fraction: 1, file: done.file, bytesUploaded: file.size });
  }

  async function run(): Promise<void> {
    try {
      if (!sha) {
        emit({ phase: "hashing", fraction: 0 });
        sha = await hashFile(file, (d, t) => emit({ phase: "hashing", fraction: t ? d / t : 0 }));
      }
      if (cancelled) return;
      if (!upload) {
        emit({ phase: "starting" });
        upload = await dataApi.createUpload(
          token,
          { name: uploadFileName(file.name), content_type: file.type || "application/octet-stream", sha256: sha, size_bytes: file.size },
          uploadIdempotencyKey(sha, file.size),
        );
        emit({ partCount: upload.part_count });
        if (upload.deduplicated && upload.file) {
          emit({ phase: "done", fraction: 1, file: upload.file, deduplicated: true, bytesUploaded: file.size });
          return;
        }
      }
      if (upload.status === "complete" && upload.file) {
        emit({ phase: "done", fraction: 1, file: upload.file, bytesUploaded: file.size });
        return;
      }
      ctl = new AbortController();
      await runParts();
      await finish();
    } catch (err) {
      if (err instanceof Paused) {
        if (!cancelled) emit({ phase: "paused" });
        return;
      }
      emit({ phase: "error", error: err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Upload failed." });
    }
  }

  return {
    start: run,
    pause() {
      ctl.abort();
    },
    resume: run,
    async cancel() {
      cancelled = true;
      ctl.abort();
      if (upload && upload.status !== "complete") {
        try {
          await dataApi.abortUpload(token, upload.id);
        } catch {
          /* best effort: the server expires abandoned uploads */
        }
      }
      emit({ phase: "cancelled" });
    },
  };
}
