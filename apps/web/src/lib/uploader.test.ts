import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";

const m = vi.hoisted(() => ({
  createUpload: vi.fn(),
  getUpload: vi.fn(),
  partUrl: vi.fn(),
  confirmPart: vi.fn(),
  completeUpload: vi.fn(),
  abortUpload: vi.fn(),
  hashFile: vi.fn(),
  hashPart: vi.fn(),
}));
vi.mock("@/lib/kits", () => ({
  dataApi: {
    createUpload: m.createUpload, getUpload: m.getUpload, partUrl: m.partUrl,
    confirmPart: m.confirmPart, completeUpload: m.completeUpload, abortUpload: m.abortUpload,
  },
}));
vi.mock("@/lib/hashing", () => ({ hashFile: m.hashFile, hashPart: m.hashPart }));

import { backoffMs, createUploader, uploadIdempotencyKey, type UploadProgress } from "./uploader";

// ---- fake XHR: each send() takes the next scripted behaviour -------------------------------------------------
type Behaviour = "ok" | "neterr" | "hang" | number; // number = HTTP status
let script: Behaviour[] = [];
let sent: Array<{ url: string; headers: Record<string, string>; size: number }> = [];

class FakeXHR {
  status = 0;
  upload: { onprogress: ((e: { loaded: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  private url = "";
  private headers: Record<string, string> = {};
  private done = false;
  open(_method: string, url: string) { this.url = url; }
  setRequestHeader(k: string, v: string) { this.headers[k] = v; }
  send(body: Blob) {
    sent.push({ url: this.url, headers: this.headers, size: body.size });
    const b = script.shift() ?? "ok";
    queueMicrotask(() => {
      if (this.done || b === "hang") return;
      this.upload.onprogress?.({ loaded: body.size });
      if (b === "neterr") { this.done = true; this.onerror?.(); return; }
      this.status = b === "ok" ? 200 : b;
      this.done = true;
      this.onload?.();
    });
  }
  abort() {
    if (this.done) return;
    this.done = true;
    this.onabort?.();
  }
}

// Poll with microtasks only: the global setTimeout is stubbed below, so vi.waitFor cannot be used.
async function until(cond: () => boolean): Promise<void> {
  for (let i = 0; i < 2000 && !cond(); i++) await Promise.resolve();
  expect(cond()).toBe(true);
}

const delays: number[] = [];
const realSetTimeout = globalThis.setTimeout;

const UPLOAD = {
  id: "u1", status: "active", size_bytes: 10, sha256: "a".repeat(64), part_size: 5, part_count: 2,
  expires_at: "", error_code: null, deduplicated: false, file: null,
};
const FILE_OUT = { id: "f1", name: "x.bin", content_type: "application/octet-stream", size_bytes: 10, sha256: "a".repeat(64), created_at: "", expires_at: null };

function make(overrides: { concurrency?: number; maxPartAttempts?: number } = {}) {
  const events: UploadProgress[] = [];
  const up = createUploader({
    token: "tok", file: new File([new Uint8Array(10)], "x.bin"), concurrency: 1, onProgress: (p) => events.push(p), ...overrides,
  });
  return { up, events, last: () => events[events.length - 1]! };
}

beforeEach(() => {
  script = [];
  sent = [];
  delays.length = 0;
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  // Backoff waits run instantly but are recorded.
  vi.stubGlobal("setTimeout", ((fn: () => void, ms?: number) => { delays.push(ms ?? 0); queueMicrotask(fn); return 0; }) as unknown as typeof setTimeout);
  Object.values(m).forEach((f) => f.mockReset());
  m.hashFile.mockResolvedValue("a".repeat(64));
  m.hashPart.mockResolvedValue("b".repeat(64));
  m.createUpload.mockResolvedValue({ ...UPLOAD });
  m.getUpload.mockResolvedValue({ upload: UPLOAD, verified_parts: [], corrupt_parts: [], pending_parts: [], missing_parts: [] });
  m.partUrl.mockImplementation(async (_t: string, _id: string, n: number) => ({ part_number: n, already_uploaded: false, method: "PUT", url: `https://store.test/p${n}`, headers: { "x-amz-meta": "1" } }));
  m.confirmPart.mockImplementation(async (_t: string, _id: string, n: number) => ({ part_number: n, status: "verified" }));
  m.completeUpload.mockResolvedValue({ ...UPLOAD, status: "complete", file: FILE_OUT });
});
afterEach(() => {
  vi.unstubAllGlobals();
  globalThis.setTimeout = realSetTimeout;
});

describe("helpers", () => {
  it("backoff doubles from 500 ms and caps at 8 s", () => {
    expect([0, 1, 2, 3, 4, 5, 9].map(backoffMs)).toEqual([500, 1000, 2000, 4000, 8000, 8000, 8000]);
  });
  it("idempotency key is stable for the same file", () => {
    expect(uploadIdempotencyKey("a".repeat(64), 10)).toBe(`up-${"a".repeat(40)}-10`);
  });
});

describe("uploader", () => {
  it("uploads every part with the presigned headers, then completes", async () => {
    const { up, last } = make();
    await up.start();
    expect(last().phase).toBe("done");
    expect(last().file).toEqual(FILE_OUT);
    expect(sent.map((s) => s.url)).toEqual(["https://store.test/p1", "https://store.test/p2"]);
    expect(sent.every((s) => s.headers["x-amz-meta"] === "1" && s.size === 5)).toBe(true);
    expect(m.createUpload.mock.calls[0]![2]).toBe(uploadIdempotencyKey("a".repeat(64), 10));
    expect(m.completeUpload).toHaveBeenCalledTimes(1);
    expect(last().retries).toBe(0);
  });

  it("skips the PUT for a part storage already has, but still confirms it", async () => {
    m.partUrl.mockImplementation(async (_t: string, _id: string, n: number) =>
      n === 1 ? { part_number: 1, already_uploaded: true } : { part_number: n, already_uploaded: false, url: "https://store.test/p2" });
    const { up, last } = make();
    await up.start();
    expect(sent).toHaveLength(1);
    expect(m.confirmPart).toHaveBeenCalledTimes(2);
    expect(last().phase).toBe("done");
  });

  it("retries a failed part with growing backoff and counts the retries", async () => {
    script = [500, "neterr", "ok", "ok"]; // part 1: 500, network error, ok; part 2: ok
    const { up, last } = make();
    await up.start();
    expect(delays).toEqual([backoffMs(0), backoffMs(1)]);
    expect(sent).toHaveLength(4);
    expect(last().retries).toBe(2);
    expect(last().phase).toBe("done");
  });

  it("stops with an error after maxPartAttempts and never completes", async () => {
    script = [500, 500, 500, 500];
    const { up, last } = make({ maxPartAttempts: 2 });
    await up.start();
    expect(last().phase).toBe("error");
    expect(last().error).toMatch(/storage returned 500/);
    expect(sent).toHaveLength(2);
    expect(m.completeUpload).not.toHaveBeenCalled();
  });

  it("re-sends a part when confirm answers 409 resend (corrupt in storage)", async () => {
    let first = true;
    m.confirmPart.mockImplementation(async (_t: string, _id: string, n: number) => {
      if (n === 1 && first) {
        first = false;
        throw new ApiError("conflict", 409, { detail: { action: "resend" } });
      }
      return { part_number: n, status: "verified" };
    });
    const { up, last } = make();
    await up.start();
    // part 1 is hashed, signed and PUT twice; part 2 once
    expect(m.partUrl.mock.calls.filter((c) => c[2] === 1)).toHaveLength(2);
    expect(sent.filter((s) => s.url.endsWith("/p1"))).toHaveLength(2);
    expect(delays).toEqual([backoffMs(0)]);
    expect(last().phase).toBe("done");
  });

  it("a non-409 confirm failure is retried too, and a hard failure surfaces", async () => {
    m.confirmPart.mockRejectedValue(new ApiError("boom", 500));
    const { up, last } = make({ maxPartAttempts: 2 });
    await up.start();
    expect(last().phase).toBe("error");
    expect(last().error).toBe("boom");
  });

  it("pause aborts the in-flight PUT; resume continues without re-creating or re-hashing the file", async () => {
    script = ["hang"]; // part 1 stalls until aborted
    const { up, events, last } = make();
    const started = up.start();
    await until(() => sent.length === 1);
    up.pause();
    await started;
    expect(last().phase).toBe("paused");
    expect(m.completeUpload).not.toHaveBeenCalled();
    expect(delays).toEqual([]); // an abort is not a failure: no backoff, no retry count
    expect(events.some((e) => e.phase === "error")).toBe(false);

    // the server says part 1 is not verified yet; resume re-reads state and finishes
    await up.resume();
    expect(last().phase).toBe("done");
    expect(m.createUpload).toHaveBeenCalledTimes(1);
    expect(m.hashFile).toHaveBeenCalledTimes(1);
    expect(m.getUpload).toHaveBeenCalledTimes(2);
    expect(m.completeUpload).toHaveBeenCalledTimes(1);
  });

  it("resume only sends the parts the server has not verified", async () => {
    script = ["hang"];
    const { up, last } = make();
    const started = up.start();
    await until(() => sent.length === 1);
    up.pause();
    await started;
    m.getUpload.mockResolvedValue({ upload: UPLOAD, verified_parts: [1], corrupt_parts: [], pending_parts: [], missing_parts: [2] });
    sent.length = 0;
    await up.resume();
    expect(sent.map((s) => s.url)).toEqual(["https://store.test/p2"]);
    expect(last().phase).toBe("done");
  });

  it("a deduplicated upload finishes without sending any part", async () => {
    m.createUpload.mockResolvedValue({ ...UPLOAD, deduplicated: true, status: "complete", file: FILE_OUT });
    const { up, last } = make();
    await up.start();
    expect(last()).toMatchObject({ phase: "done", deduplicated: true });
    expect(sent).toHaveLength(0);
    expect(m.completeUpload).not.toHaveBeenCalled();
  });

  it("cancel aborts the server upload and reports cancelled", async () => {
    script = ["hang"];
    const { up, last } = make();
    const started = up.start();
    await until(() => sent.length === 1);
    await up.cancel();
    await started;
    expect(m.abortUpload).toHaveBeenCalledWith("tok", "u1");
    expect(last().phase).toBe("cancelled");
  });
});
