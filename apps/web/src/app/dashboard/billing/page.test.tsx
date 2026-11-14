// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/dashboard/billing",
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ accessToken: "t", isReady: true, isAuthenticated: true }),
}));
vi.mock("@/components/console/toast", () => ({
  useToast: () => ({ toast: vi.fn(), push: vi.fn(), success: vi.fn(), error: vi.fn() }),
}));

const getLedger = vi.fn();
const getBillingAccount = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getLedger: (...a: unknown[]) => getLedger(...a),
      getBillingAccount: (...a: unknown[]) => getBillingAccount(...a),
    },
  };
});

import BillingPage from "./page";

const entries = (from: number, count: number) =>
  Array.from({ length: count }, (_, i) => ({
    entry_id: `e-${from + i}`,
    transaction_id: `t-${from + i}`,
    kind: "capture",
    direction: "debit",
    amount: `${from + i}.00`,
    currency: "credits",
    reference_type: "job",
    reference_id: null,
    created_at: new Date().toISOString(),
  }));

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <BillingPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getBillingAccount.mockResolvedValue({ balance: "10.00", available: "10.00", reserved: "0.00", currency: "credits" });
});
afterEach(() => {
  cleanup();
  getLedger.mockReset();
  getBillingAccount.mockReset();
});

describe("Billing history", () => {
  it("lets a person reach entries older than the first page", async () => {
    getLedger.mockResolvedValueOnce(entries(1, 50)).mockResolvedValueOnce(entries(51, 3));
    renderPage();

    const more = await screen.findByRole("button", { name: "Show older entries" });
    expect(getLedger).toHaveBeenCalledWith("t", { page: 1, page_size: 50 });
    expect(screen.getAllByRole("row")).toHaveLength(51); // header + 50 entries

    fireEvent.click(more);
    await waitFor(() => expect(getLedger).toHaveBeenCalledWith("t", { page: 2, page_size: 50 }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(54)); // header + 53 entries
    // A short last page is the end: nothing more to load.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Show older entries" })).toBeNull());
  });

  it("shows no 'older entries' button when everything fits on one page", async () => {
    getLedger.mockResolvedValue(entries(1, 3));
    renderPage();
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(4));
    expect(screen.queryByRole("button", { name: "Show older entries" })).toBeNull();
    expect(getLedger).toHaveBeenCalledTimes(1);
  });

  it("keeps the amounts as the server wrote them", async () => {
    getLedger.mockResolvedValue(entries(7, 1));
    renderPage();
    const row = (await screen.findByText("−7.00")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Taken")).toBeTruthy();
  });
});
