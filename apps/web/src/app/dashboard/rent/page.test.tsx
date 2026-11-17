// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/dashboard/rent",
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ isReady: true, isAuthenticated: true, isAdmin: false, accessToken: "t", email: "a@b.c", user: null }),
}));

import RentPage from "./page";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RentPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {}))); // no network: every request stays pending
});
afterEach(() => cleanup());

describe("Rent page: graphics cards", () => {
  it("offers 0 (CPU only) and 1 to 8, and still starts on 1", () => {
    renderPage();
    const select = screen.getByLabelText("Graphics cards") as HTMLSelectElement;
    const options = Array.from(select.options).map((o) => [o.value, o.textContent]);
    expect(options).toEqual([
      ["0", "0 (no graphics card, CPU only)"],
      ["1", "1"], ["2", "2"], ["3", "3"], ["4", "4"], ["5", "5"], ["6", "6"], ["7", "7"], ["8", "8"],
    ]);
    expect(select.value).toBe("1"); // the default is unchanged
  });

  it("explains CPU-only only while 0 is chosen", () => {
    renderPage();
    const select = screen.getByLabelText("Graphics cards") as HTMLSelectElement;
    expect(screen.queryByText(/No graphics card: only the computer's processor and memory/)).toBeNull();
    fireEvent.change(select, { target: { value: "0" } });
    expect(screen.getByText(/No graphics card: only the computer's processor and memory/)).toBeTruthy();
    expect(select.getAttribute("aria-describedby")).toBe("gpus-d");
    fireEvent.change(select, { target: { value: "2" } });
    expect(screen.queryByText(/No graphics card: only the computer's processor and memory/)).toBeNull();
    expect(select.getAttribute("aria-describedby")).toBeNull();
  });

  it("shows the same hold estimate for 0 cards as for 1 card", () => {
    renderPage();
    const select = screen.getByLabelText("Graphics cards") as HTMLSelectElement;
    const note = () => document.body.textContent?.match(/set aside up to about ([0-9.]+)/)?.[1];
    const one = note();
    expect(one).toBeTruthy();
    fireEvent.change(select, { target: { value: "0" } });
    expect(note()).toBe(one);
    fireEvent.change(select, { target: { value: "2" } });
    expect(Number(note())).toBeCloseTo(Number(one) * 2, 6);
  });

  it("keeps both original ways to start, and the warning", () => {
    renderPage();
    expect(screen.getByLabelText("My own image")).toBeTruthy();
    expect(screen.getByLabelText("A ready-made template")).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: /I understand/i })).toBeTruthy();
  });

  it("fills the form from the Python terminal button, and the Jupyter button explains why it is off here", () => {
    renderPage();
    const python = screen.getByRole("button", { name: "Python terminal" });
    const jupyter = screen.getByRole("button", { name: "Jupyter web page" }) as HTMLButtonElement;
    expect(jupyter.disabled).toBe(true); // no session web address in the test setup
    expect(document.body.textContent).toMatch(/session web address is not switched on/);
    fireEvent.click(python);
    expect((screen.getByLabelText("Image address") as HTMLInputElement).value).toMatch(/^docker\.io\/library\/python@sha256:[0-9a-f]{64}$/);
    expect((screen.getByLabelText("Start command (optional)") as HTMLInputElement).value).toBe("/bin/bash");
    expect((screen.getByLabelText("Graphics cards") as HTMLSelectElement).value).toBe("0");
    expect((screen.getByLabelText(/A terminal/) as HTMLInputElement).checked).toBe(true);
  });

  it("still lets the customer type the image address by hand", () => {
    renderPage();
    const box = screen.getByLabelText("Image address") as HTMLInputElement;
    fireEvent.change(box, { target: { value: "ghcr.io/acme/x@sha256:" + "a".repeat(64) } });
    expect(box.value).toBe("ghcr.io/acme/x@sha256:" + "a".repeat(64));
  });

  it("tells an unconfirmed account to confirm its email, instead of a vague error", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/v1/data/files")) {
        return Promise.resolve(new Response(JSON.stringify({ detail: "email_not_verified" }), { status: 403, headers: { "Content-Type": "application/json" } }));
      }
      return new Promise(() => {});
    }));
    renderPage();
    expect(await screen.findByText(/Please confirm your email address to bring in your files/)).toBeTruthy();
    expect(document.body.textContent).toMatch(/You can still start the rental without them\./);
  });

  it("labels a Windows computer in the list and warns before a customer rents it", async () => {
    const m = (id: string, os: string | null) => ({
      machine_id: id, gpu_model: "RTX 3060", gpu_vendor: "nvidia", gpu_runtime: "cuda", gpu_count: 1, vram_mb: 12288, cpu_model: null, cpu_cores: 8,
      ram_mb: 16384, region: null, verification_status: "detected", price_per_hour: "0.50", currency: "credits", supported_workloads: [],
      available_slots: 1, max_concurrent_jobs: 1, reliability: { completed_jobs: 0, success_rate: null }, last_heartbeat_at: new Date().toISOString(),
      host_kind: "shared", container_rental: true, host_os: os,
    });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("/v1/marketplace/machines")) {
        return Promise.resolve(new Response(JSON.stringify({ items: [m("win-1", "windows"), m("lin-1", "linux")], page: 1, page_size: 100, total: 2 }), {
          status: 200, headers: { "Content-Type": "application/json" },
        }));
      }
      return new Promise(() => {});
    }));
    renderPage();
    const select = (await screen.findByLabelText("Computer")) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(3));
    const labels = Array.from(select.options).map((o) => o.textContent ?? "");
    expect(labels.filter((t) => t.includes("Windows PC (experimental)"))).toHaveLength(1);
    expect(screen.queryByTestId("windows-host-warning")).toBeNull();
    fireEvent.change(select, { target: { value: "lin-1" } });
    expect(screen.queryByTestId("windows-host-warning")).toBeNull();
    fireEvent.change(select, { target: { value: "win-1" } });
    expect(screen.getByTestId("windows-host-warning").textContent).toMatch(/Windows rentals are experimental/);
    expect(screen.getByTestId("windows-host-warning").textContent).toMatch(/only Linux images work/);
  });

  it("tells a customer that a Windows computer gives all its cards together, and fixes the count on one press", async () => {
    const m = (id: string, os: string, cards: number) => ({
      machine_id: id, gpu_model: "RTX 3060", gpu_vendor: "nvidia", gpu_runtime: "cuda", gpu_count: cards, vram_mb: 12288, cpu_model: null, cpu_cores: 8,
      ram_mb: 16384, region: null, verification_status: "detected", price_per_hour: "0.50", currency: "credits", supported_workloads: [],
      available_slots: 1, max_concurrent_jobs: 1, reliability: { completed_jobs: 0, success_rate: null }, last_heartbeat_at: new Date().toISOString(),
      host_kind: "shared", container_rental: true, host_os: os,
    });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("/v1/marketplace/machines")) {
        return Promise.resolve(new Response(JSON.stringify({ items: [m("win-2", "windows", 2), m("win-1", "windows", 1), m("lin-4", "linux", 4)], page: 1, page_size: 100, total: 3 }), {
          status: 200, headers: { "Content-Type": "application/json" },
        }));
      }
      return new Promise(() => {});
    }));
    renderPage();
    const select = (await screen.findByLabelText("Computer")) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(4));
    const cards = screen.getByLabelText("Graphics cards") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "win-2" } });
    fireEvent.change(cards, { target: { value: "1" } });
    expect(screen.getByTestId("windows-host-all-cards").textContent).toMatch(/This one has 2: choose 2/);
    fireEvent.click(screen.getByRole("button", { name: "Use 2 graphics cards" }));
    expect(cards.value).toBe("2");
    expect(screen.queryByTestId("windows-host-all-cards")).toBeNull();
    fireEvent.change(cards, { target: { value: "0" } }); // none is always allowed
    expect(screen.queryByTestId("windows-host-all-cards")).toBeNull();
    fireEvent.change(select, { target: { value: "win-1" } }); // a one-card Windows PC: 1 is right
    fireEvent.change(cards, { target: { value: "1" } });
    expect(screen.queryByTestId("windows-host-all-cards")).toBeNull();
    fireEvent.change(cards, { target: { value: "2" } });
    expect(screen.getByRole("button", { name: "Use 1 graphics card" })).toBeTruthy();
    fireEvent.change(select, { target: { value: "lin-4" } }); // Linux picks cards by number: no rule
    expect(screen.queryByTestId("windows-host-all-cards")).toBeNull();
  });
});

