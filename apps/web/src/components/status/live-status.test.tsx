// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LiveStatus } from "./live-status";

function show() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <LiveStatus />
    </QueryClientProvider>,
  );
}
const health = (status: number, body: unknown) =>
  vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LiveStatus", () => {
  it("does not report a deployment without Redis as broken", async () => {
    vi.stubGlobal("fetch", health(200, { status: "ok", db: "up", redis: "not_configured" }));
    show();
    await waitFor(() => expect(screen.getAllByText("Working")).toHaveLength(3));
    expect(screen.queryByText("Not working")).toBeNull();
  });

  it("shows which part is down when /health answers 503 (degraded) instead of saying everything is unreachable", async () => {
    vi.stubGlobal("fetch", health(503, { status: "degraded", db: "down", redis: "up" }));
    show();
    await waitFor(() => expect(screen.getAllByText("Not working").length).toBe(2));
    expect(screen.getAllByText("Working")).toHaveLength(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
