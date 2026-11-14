// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/dashboard/jobs/new",
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ accessToken: "test-token", isReady: true, isAuthenticated: true }),
}));

const createJob = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, createJob: (...args: unknown[]) => createJob(...args) } };
});

import NewJobPage from "./page";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NewJobPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  createJob.mockResolvedValue({ job_id: "11111111-1111-1111-1111-111111111111" });
});

afterEach(() => {
  cleanup();
  createJob.mockReset();
  push.mockReset();
});

function advancedSection(): HTMLDetailsElement {
  const el = document.querySelector("details");
  if (!el) throw new Error("the More options section is missing");
  return el as HTMLDetailsElement;
}

describe("Start a new job page", () => {
  it("shows the two numbered steps and keeps the expert options folded away", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "What do you want to do?" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "How much are you willing to spend?" })).toBeTruthy();
    expect(advancedSection().open).toBe(false);
    // The expert settings still exist in the page (so they are submitted), just folded away.
    expect(advancedSection().querySelector("#isolation_tier")).toBeTruthy();
    expect(advancedSection().querySelector("#required_gpu_runtime")).toBeTruthy();
    expect(advancedSection().querySelector("#max_retries")).toBeTruthy();
  });

  it("offers the job type as two clear choices, one prompt or a list, and switches the editor", () => {
    renderPage();
    const one = screen.getByLabelText(/Ask one question/) as HTMLInputElement;
    const many = screen.getByLabelText(/Ask many questions at once/) as HTMLInputElement;
    expect(one.checked).toBe(true);
    expect(document.getElementById("prompt")).toBeTruthy();
    expect(document.getElementById("items_json")).toBeNull();

    fireEvent.click(many);
    expect(many.checked).toBe(true);
    expect(document.getElementById("items_json")).toBeTruthy();
    expect(document.getElementById("prompt")).toBeNull();
  });

  it("still refuses an empty prompt with a plain message", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Submit job" }));
    expect(await screen.findByText(/Write a prompt first/)).toBeTruthy();
    expect(createJob).not.toHaveBeenCalled();
  });

  it("opens the folded options by itself when a problem is inside them", async () => {
    renderPage();
    fireEvent.change(document.getElementById("prompt") as HTMLTextAreaElement, { target: { value: "Hello" } });
    // Break a setting that lives inside the folded section.
    fireEvent.change(document.getElementById("gpu_count") as HTMLInputElement, { target: { value: "0" } });
    expect(advancedSection().open).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Submit job" }));
    expect(await screen.findByText("Enter 1 or more.")).toBeTruthy();
    await waitFor(() => expect(advancedSection().open).toBe(true));
    expect(createJob).not.toHaveBeenCalled();
  });

  it("sends the same job as before: the defaults, and the dedicated-computer choice when ticked", async () => {
    renderPage();
    fireEvent.change(document.getElementById("prompt") as HTMLTextAreaElement, { target: { value: "  Write a poem  " } });
    fireEvent.click(document.getElementById("require_dedicated_host") as HTMLInputElement);
    fireEvent.click(screen.getByRole("button", { name: "Submit job" }));

    await waitFor(() => expect(createJob).toHaveBeenCalledTimes(1));
    const [token, payload] = createJob.mock.calls[0];
    expect(token).toBe("test-token");
    expect(payload).toMatchObject({
      workload_type: "text-generation",
      isolation_tier: "container",
      required_gpu_runtime: "cuda",
      model: "echo-sim-v1",
      input: { prompt: "Write a poem", max_tokens: 64 },
      gpu_count: 1,
      estimated_minutes: 5,
      max_cost: "5.00",
      max_runtime_seconds: 300,
      retry_policy: { max_retries: 1 },
      data_policy: { require_dedicated_host: true },
    });
    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard/jobs/11111111-1111-1111-1111-111111111111"));
  });

  it("tells the person the most they can be charged, and keeps it in step with the box", () => {
    renderPage();
    expect(screen.getByText(/The most you can be charged is/).textContent).toContain("5.00 credits");
    fireEvent.change(document.getElementById("max_cost") as HTMLInputElement, { target: { value: "12.50" } });
    expect(screen.getByText(/The most you can be charged is/).textContent).toContain("12.50 credits");
    fireEvent.change(document.getElementById("max_cost") as HTMLInputElement, { target: { value: "abc" } });
    expect(screen.getByText(/The most you can be charged is/).textContent).toContain("the amount you enter above");
  });

  it("shows how long the time limit is in minutes", () => {
    renderPage();
    expect(screen.getByText(/The job is stopped after this long \(5 minutes\)/)).toBeTruthy();
  });
});
