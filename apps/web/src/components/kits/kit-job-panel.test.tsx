// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const JOB = "11111111-1111-1111-1111-111111111111";

function respond(url: string): Response {
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  if (url.includes("/artifacts")) return json([]); // a Kit job has no v1 artifact rows
  if (url.includes("/logs")) return json({ job_id: JOB, lines: [] });
  if (url.includes("/v1/data/files")) {
    return json([{ id: "f1", name: `out/${JOB}/frames`, content_type: "application/octet-stream", size_bytes: 2048, sha256: "a".repeat(64), created_at: "2026-01-01T00:00:00Z", expires_at: null }]);
  }
  return json({});
}

describe("KitJobPanel results", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(respond(String(input))));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => cleanup());

  it("lists the job's outputs from the file store (out/<job>/<name>) with a Download button", async () => {
    const { KitJobPanel } = await import("./kit-job-panel");
    const job = { job_id: JOB, workload_type: "kit", model: "qa@1.0.0", status: "completed", execution_mode: "hardware", created_at: "2026-01-01T00:00:00Z" };
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <KitJobPanel job={job as never} token="t" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("frames")).toBeTruthy());
    expect(screen.getAllByRole("button", { name: /download/i }).length).toBe(1);
    expect(screen.queryByText(/did not make any files/i)).toBeNull();
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/v1/data/files") && u.includes(`name_prefix=out%2F${JOB}%2F`))).toBe(true);
  });
});
