// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/admin/jobs" }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "t", isReady: true, isAuthenticated: true, isAdmin: true }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listAllAdminJobs: () =>
        Promise.resolve([
          {
            job_id: "abcdef12-0000-0000-0000-000000000000",
            customer_id: "c1",
            workload_type: "text-generation",
            model: "echo-sim-v1",
            status: "completed",
            execution_mode: "simulated",
            created_at: new Date().toISOString(),
          },
        ]),
    },
  };
});

import AdminJobsPage from "./page";

afterEach(cleanup);

describe("Admin jobs list", () => {
  it("opens the ADMIN job view, not the customer-only page that shows 'not found' for other people's jobs", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AdminJobsPage />
      </QueryClientProvider>,
    );
    const link = await screen.findByRole("link", { name: /abcdef12/ });
    expect(link.getAttribute("href")).toBe("/admin/jobs/abcdef12-0000-0000-0000-000000000000");
  });
});
