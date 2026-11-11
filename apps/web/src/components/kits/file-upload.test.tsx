// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { UploadProgress, UploaderOptions } from "@/lib/uploader";

const created: { opts: UploaderOptions; cancel: ReturnType<typeof vi.fn> }[] = [];
vi.mock("@/lib/uploader", () => ({
  createUploader: (opts: UploaderOptions) => {
    const cancel = vi.fn(async () => {});
    created.push({ opts, cancel });
    return { start: vi.fn(async () => {}), pause: vi.fn(), resume: vi.fn(async () => {}), cancel };
  },
}));

import { FileUpload } from "./file-upload";

afterEach(() => {
  cleanup();
  created.length = 0;
});

const done = (id: string): UploadProgress => ({
  phase: "done", fraction: 1, bytesUploaded: 1, totalBytes: 1, partsDone: 1, partCount: 1, retries: 0,
  file: { id, name: id, content_type: "x", size_bytes: 1, sha256: "s", created_at: "", expires_at: null },
});

describe("FileUpload", () => {
  it("cancels the previous upload when another file is chosen and ignores its late progress", () => {
    const onUploaded = vi.fn();
    const { container } = render(<FileUpload label="Audio" token="t" onUploaded={onUploaded} onCleared={() => {}} />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    fireEvent.change(input, { target: { files: [new File(["a"], "a.bin")] } });
    fireEvent.change(input, { target: { files: [new File(["b"], "b.bin")] } });
    expect(created).toHaveLength(2);
    expect(created[0].cancel).toHaveBeenCalledTimes(1);

    // the first upload was still hashing and now finishes: its file must not become the input
    act(() => created[0].opts.onProgress(done("file-a")));
    expect(onUploaded).not.toHaveBeenCalled();

    act(() => created[1].opts.onProgress(done("file-b")));
    expect(onUploaded).toHaveBeenCalledTimes(1);
    expect(onUploaded.mock.calls[0][0].id).toBe("file-b");
    expect(screen.getByText("b.bin")).toBeTruthy();
  });
});
