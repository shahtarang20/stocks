// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/marketplace/gpus",
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ accessToken: null, isReady: true, isAuthenticated: false }),
}));

const searchMachines = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, searchMachines: (...args: unknown[]) => searchMachines(...args) } };
});

import { LiveGpuTable, MEMORY_CHOICES } from "./live-gpu-table";

const machine = {
  machine_id: "m-1",
  gpu_model: "RTX 4090",
  gpu_vendor: "nvidia",
  gpu_runtime: "cuda",
  gpu_count: 1,
  vram_mb: 24564,
  cpu_model: "AMD EPYC",
  cpu_cores: 16,
  ram_mb: 65536,
  region: "us-east",
  verification_status: "detected",
  price_per_hour: "1.50",
  currency: "credits",
  supported_workloads: ["text-generation"],
  available_slots: 2,
  max_concurrent_jobs: 4,
  reliability: { completed_jobs: 7, success_rate: 0.9 },
  last_heartbeat_at: new Date().toISOString(),
  host_kind: "dedicated",
};

function renderTable() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LiveGpuTable />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  searchMachines.mockResolvedValue({ items: [machine], page: 1, page_size: 20, total: 1 });
});
afterEach(() => {
  cleanup();
  searchMachines.mockReset();
});

const lastParams = () => searchMachines.mock.calls[searchMachines.mock.calls.length - 1][0];

describe("Marketplace computers table", () => {
  it("shows six clear columns, with no repeated 'Hardware check' column", async () => {
    renderTable();
    await screen.findByText("RTX 4090");
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent?.trim());
    expect(headers).toEqual([
      "Graphics card (GPU)",
      "Computer",
      "Price per hour",
      "Free now",
      "Track record",
      "Action",
    ]);
    expect(screen.queryByText("Hardware check")).toBeNull();
    expect(document.getElementById("f-verif")).toBeNull();
  });

  it("puts the essentials of each computer where a person looks for them", async () => {
    renderTable();
    await screen.findByText("RTX 4090");
    const row = screen.getAllByRole("row")[1];
    expect(within(row).getByText("1.50")).toBeTruthy(); // price shown verbatim
    expect(within(row).getByText("2 of 4")).toBeTruthy(); // free spots
    expect(within(row).getByText("Dedicated")).toBeTruthy();
    expect(within(row).getByText(/us-east/)).toBeTruthy(); // region sits with the computer
    expect(within(row).getByText(/90% finished well/)).toBeTruthy();
    expect(within(row).getByRole("button", { name: "Get a price quote" })).toBeTruthy();
  });

  it("asks for graphics card memory in gigabytes and sends the matching megabytes", async () => {
    renderTable();
    await screen.findByText("RTX 4090");
    expect((document.getElementById("f-vram") as HTMLSelectElement).tagName).toBe("SELECT");
    fireEvent.change(document.getElementById("f-vram") as HTMLSelectElement, { target: { value: "23040" } });
    await waitFor(() => expect(lastParams().min_vram_mb).toBe(23040));
    // Thresholds sit just below the marketed size, so a real 24 GB card (about 24 564 MB) still matches.
    expect(MEMORY_CHOICES.find((c) => c.label === "24 GB or more")!.mb).toBeLessThan(24564);
  });

  it("keeps the rarely used filters folded away and counts the ones that are on", async () => {
    renderTable();
    await screen.findByText("RTX 4090");
    const more = document.querySelector("details") as HTMLDetailsElement;
    expect(more.open).toBe(false);
    expect(screen.queryByText(/\d on$/)).toBeNull();

    fireEvent.change(document.getElementById("f-region") as HTMLInputElement, { target: { value: "us-east" } });
    await waitFor(() => expect(lastParams().region).toBe("us-east"));
    expect(screen.getByText("1 on")).toBeTruthy();
  });

  it("still filters by kind of computer, and Reset clears everything", async () => {
    renderTable();
    await screen.findByText("RTX 4090");
    fireEvent.change(document.getElementById("f-kind") as HTMLSelectElement, { target: { value: "dedicated" } });
    await waitFor(() => expect(lastParams().host_kind).toBe("dedicated"));

    fireEvent.click(screen.getByRole("button", { name: "Reset filters" }));
    await waitFor(() => expect(lastParams().host_kind).toBeUndefined());
    expect((document.getElementById("f-kind") as HTMLSelectElement).value).toBe("");
    expect((document.getElementById("f-vram") as HTMLSelectElement).value).toBe("");
  });
});
