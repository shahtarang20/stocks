// W9 (cross-platform plan): the browser uploader on Windows. WRITTEN, NOT RUN.
// Pins: the name sent to the server is a bare file name for both Windows and POSIX separators; bytes are never
// transformed (no newline translation); the idempotency key does not depend on the name or the OS.
// Bug re-insertion: send file.name unchanged (the "C:\\dir" test fails); build the key from the name (the key test fails).
import { beforeEach, describe, expect, it, vi } from "vitest";

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

import { createUploader, uploadFileName, uploadIdempotencyKey } from "./uploader";

describe("uploadFileName", () => {
  it("keeps a plain name", () => {
    expect(uploadFileName("scene.blend")).toBe("scene.blend");
    expect(uploadFileName("my file (1).zip")).toBe("my file (1).zip");
  });

  it("strips Windows paths, drive letters and UNC prefixes", () => {
    expect(uploadFileName("C:\\Users\\me\\Desktop\\scene.blend")).toBe("scene.blend");
    expect(uploadFileName("\\\\server\\share\\a.bin")).toBe("a.bin");
    expect(uploadFileName("dir\\sub\\a.bin")).toBe("a.bin");
  });

  it("strips POSIX and mixed paths", () => {
    expect(uploadFileName("/home/me/a.bin")).toBe("a.bin");
    expect(uploadFileName("a/b\\c/d.txt")).toBe("d.txt");
  });

  it("never returns an empty name and never keeps a separator", () => {
    expect(uploadFileName("")).toBe("upload.bin");
    expect(uploadFileName("dir\\")).toBe("upload.bin");
    expect(uploadFileName("/")).toBe("upload.bin");
    for (const n of ["a\\b", "a/b", "C:\\x\\", "..\\..\\evil.bin"]) expect(uploadFileName(n)).not.toMatch(/[\\/]/);
  });

  it("keeps non-ASCII names untouched (browsers give UTF-16 strings on every OS)", () => {
    expect(uploadFileName("C:\\Users\\Jos\u00e9\\\u30c7\u30fc\u30bf.csv")).toBe("\u30c7\u30fc\u30bf.csv");
  });
});

describe("uploader on a Windows-style file", () => {
  beforeEach(() => {
    Object.values(m).forEach((f) => f.mockReset());
    m.hashFile.mockResolvedValue("c".repeat(64));
    m.createUpload.mockResolvedValue({
      id: "u1", status: "complete", size_bytes: 4, sha256: "c".repeat(64), part_size: 4, part_count: 1,
      expires_at: "", error_code: null, deduplicated: true,
      file: { id: "f1", name: "data.csv", content_type: "text/csv", size_bytes: 4, sha256: "c".repeat(64), created_at: "", expires_at: null },
    });
  });

  it("sends a bare name, the browser's content type and the exact byte count (CRLF kept)", async () => {
    const bytes = new Uint8Array([0x61, 0x0d, 0x0a, 0x62]); // "a\r\nb": the bytes are never newline-translated
    const file = new File([bytes], "C:\\Users\\me\\data.csv", { type: "text/csv" });
    const events: Array<{ phase: string }> = [];
    const up = createUploader({ token: "t", file, onProgress: (p) => events.push(p) });
    await up.start();
    const [, body, key] = m.createUpload.mock.calls[0]!;
    expect(body).toEqual({ name: "data.csv", content_type: "text/csv", sha256: "c".repeat(64), size_bytes: 4 });
    expect(key).toBe(uploadIdempotencyKey("c".repeat(64), 4));
    expect(events[events.length - 1]!.phase).toBe("done");
  });

  it("falls back to application/octet-stream when Windows has no type registered for the extension", async () => {
    const file = new File([new Uint8Array(4)], "scene.blend"); // Windows leaves type "" for unregistered extensions
    await createUploader({ token: "t", file, onProgress: () => {} }).start();
    expect(m.createUpload.mock.calls[0]![1].content_type).toBe("application/octet-stream");
  });

  it("the idempotency key depends only on the content hash and size, never on the name or OS", () => {
    expect(uploadIdempotencyKey("d".repeat(64), 7)).toBe(`up-${"d".repeat(40)}-7`);
  });
});
