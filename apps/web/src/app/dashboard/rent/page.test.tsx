// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
});
