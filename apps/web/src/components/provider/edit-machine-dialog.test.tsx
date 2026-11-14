// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ accessToken: "t", isReady: true, isAuthenticated: true }),
}));
vi.mock("@/components/console/toast", () => ({
  useToast: () => ({ toast: vi.fn(), push: vi.fn(), success: vi.fn(), error: vi.fn() }),
}));

import { EditMachineDialog } from "./edit-machine-dialog";
import type { Machine } from "@/lib/api";

const machine = {
  machine_id: "m-1",
  hostname: "gpu-box",
  availability: "online",
  asking_price_per_hour: "1.50",
  max_concurrent_jobs: 2,
  region: "us-east",
} as unknown as Machine;

afterEach(cleanup);

describe("Host price field", () => {
  it("is in credits, the same unit customers see, and not in dollars", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EditMachineDialog machine={machine} open onOpenChange={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(screen.getByLabelText("Price per hour (credits)")).toBeTruthy();
    expect(screen.queryByText(/USD/)).toBeNull();
    expect(screen.getByText(/test credits, not real money/)).toBeTruthy();
  });
});
