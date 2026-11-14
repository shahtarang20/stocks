// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useParams: () => ({ jobId: "11111111-2222-3333-4444-555555555555" }),
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/admin/jobs/11111111-2222-3333-4444-555555555555",
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ accessToken: "admin-token", isReady: true, isAuthenticated: true, isAdmin: true }),
}));

const getAdminJob = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, getAdminJob: (...a: unknown[]) => getAdminJob(...a) } };
});

import AdminJobDetailPage from "./page";
import { ApiError } from "@/lib/api";

const now = new Date().toISOString();
const job = {
  job_id: "11111111-2222-3333-4444-555555555555",
  customer_id: "99999999-0000-0000-0000-000000000001",
  customer_email: "customer@example.com",
  workload_type: "text-generation",
  model: "echo-sim-v1",
  status: "failed",
  execution_mode: "hardware",
  isolation_tier: "container",
  required_gpu_runtime: "cuda",
  gpu_count: 1,
  estimated_minutes: 5,
  max_cost: "5.00",
  final_cost: null,
  max_runtime_seconds: 300,
  retry_policy: { max_retries: 1 },
  attempts_made: 1,
  cancel_requested: false,
  machine_id: "aaaaaaaa-0000-0000-0000-000000000002",
  machine_hostname: "gpu-box-7",
  host_kind: "shared",
  error_code: "thermal_shutdown",
  error_message: "GPU temperature 95 C reached the hard stop",
  verification_valid: false,
  verification_reason: "agent_reported_failure",
  created_at: now,
  updated_at: now,
  started_at: now,
  finished_at: now,
  lease_expires_at: null,
  events: [
    { seq: 2, from_state: "reserved", to_state: "running", reason: "claimed", actor: "agent", created_at: now },
    { seq: 1, from_state: null, to_state: "reserved", reason: "scheduled", actor: "system", created_at: now },
    { seq: 3, from_state: "running", to_state: "failed", reason: "agent_failed", actor: "agent", created_at: now },
  ],
  attempts: [
    { attempt_number: 1, machine_id: "aaaaaaaa-0000-0000-0000-000000000002", status: "failed", error_code: "thermal_shutdown", started_at: now, finished_at: now },
  ],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AdminJobDetailPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => getAdminJob.mockResolvedValue(job));
afterEach(() => {
  cleanup();
  getAdminJob.mockReset();
});

describe("Admin job page", () => {
  it("asks the admin endpoint for the job in the address, with the admin's token", async () => {
    renderPage();
    await screen.findByText("customer@example.com");
    expect(getAdminJob).toHaveBeenCalledWith("admin-token", "11111111-2222-3333-4444-555555555555");
  });

  it("shows who, where, what it cost and what went wrong", async () => {
    renderPage();
    expect(await screen.findByText("customer@example.com")).toBeTruthy();
    expect(screen.getByText(/gpu-box-7/)).toBeTruthy();
    expect(screen.getByText(/Shared PC \(its owner also uses it\)/)).toBeTruthy();
    expect(screen.getByText("Not charged")).toBeTruthy(); // no final cost yet
    const problem = screen.getByRole("alert");
    expect(within(problem).getByText("thermal_shutdown")).toBeTruthy();
    expect(within(problem).getByText(/GPU temperature 95 C reached the hard stop/)).toBeTruthy();
  });

  it("tells the admin that the customer's content is not shown here", async () => {
    renderPage();
    const note = (await screen.findByRole("note")).textContent ?? "";
    expect(note).toMatch(/input and results are not shown/i);
    expect(note).toMatch(/audit log/i);
  });

  it("lists the steps in order and each attempt", async () => {
    renderPage();
    await screen.findByText("customer@example.com");
    const steps = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    const order = ["Scheduled", "Claimed", "Agent failed"].map((word) => steps.findIndex((s) => s.includes(word)));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order); // sequence 1, 2, 3 even though they arrived out of order
    expect(screen.getByText("Attempt 1")).toBeTruthy();
  });

  it("shows a measured shared graphics card with the count, and says nothing when nothing was measured", async () => {
    getAdminJob.mockResolvedValue({ ...job, gpu_shared: true, gpu_other_processes: 2 });
    renderPage();
    expect(await screen.findByText(/Shared with another program \(2 seen\)/)).toBeTruthy();
    cleanup();

    getAdminJob.mockResolvedValue({ ...job, gpu_shared: null, gpu_other_processes: null });
    renderPage();
    await screen.findByText("customer@example.com");
    expect(screen.queryByText(/Shared with another program/)).toBeNull();
    expect(screen.queryByText(/alone on the/i)).toBeNull(); // no claim either way
  });

  it("says plainly when the job does not exist", async () => {
    getAdminJob.mockRejectedValue(new ApiError("Job not found", 404));
    renderPage();
    expect(await screen.findByText("We could not find that job")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
  });
});
