// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn() }),
  usePathname: () => "/dashboard/sessions",
}));

const list = vi.fn();
const create = vi.fn();
vi.mock("@/lib/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sessions")>();
  return {
    ...actual,
    sessionsApi: { ...actual.sessionsApi, list: (...a: unknown[]) => list(...a), create: (...a: unknown[]) => create(...a) },
  };
});

const listKits = vi.fn();
vi.mock("@/lib/kits", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kits")>();
  return { ...actual, kitsApi: { ...actual.kitsApi, listKits: (...a: unknown[]) => listKits(...a) } };
});

vi.mock("@/lib/session-crypto", () => ({
  newClientKey: () => ({ pubHex: "ab".repeat(32), priv: new Uint8Array(32) }),
  storeClientKey: vi.fn(),
}));

import SessionsPage from "./page";

const kit = (slug: string, name: string, version: string, mode: string) => ({
  slug,
  name,
  version,
  category: "other",
  status: "published",
  image_digest: "sha256:" + "a".repeat(64),
  mode,
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SessionsPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  list.mockResolvedValue([]);
  create.mockResolvedValue({ id: "s-1" });
  listKits.mockResolvedValue([
    kit("notebooks", "JupyterLab notebooks", "1.2.0", "session"),
    kit("upscale", "Image upscaling", "1.0.0", "batch"),
    kit("llm-serve", "LLM serving", "1.0.0", "service"),
  ]);
});
afterEach(() => {
  cleanup();
  list.mockReset();
  create.mockReset();
  listKits.mockReset();
  push.mockReset();
  window.history.replaceState(null, "", "/");
});

describe("Sessions page: choosing what to open", () => {
  it("offers only the Kits that can run as a session, by their real names", async () => {
    renderPage();
    await screen.findByRole("option", { name: /JupyterLab notebooks/ });
    const select = screen.getByLabelText("Which Kit to run") as HTMLSelectElement;
    expect(select.tagName).toBe("SELECT");
    const options = Array.from(select.options).map((o) => o.textContent);
    expect(options).toEqual(["JupyterLab notebooks (version 1.2.0)"]);
    // It starts on the first Kit, so the person can press Start straight away, and it says in plain words what that is.
    await waitFor(() => expect(select.value).toBe("notebooks@1.2.0"));
    expect(screen.getByText("Open a notebook in your browser on a computer with a graphics card.")).toBeTruthy();
  });

  it("keeps the Kit a link asked for, even if it is not in the list", async () => {
    window.history.replaceState(null, "", "/dashboard/sessions?kit=custom-tool@2.0.0");
    renderPage();
    await screen.findByRole("option", { name: /JupyterLab notebooks/ });
    const select = screen.getByLabelText("Which Kit to run") as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe("custom-tool@2.0.0"));
    expect(Array.from(select.options).map((o) => o.value)).toContain("notebooks@1.2.0");
  });

  it("starts the session with the picked Kit and the limits, and opens it", async () => {
    renderPage();
    await screen.findByRole("option", { name: /JupyterLab notebooks/ });
    const select = screen.getByLabelText("Which Kit to run") as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe("notebooks@1.2.0"));
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({
      kit_ref: "notebooks@1.2.0",
      gpu_count: 1,
      max_price_per_gpu_hour: "1.00",
      max_duration_seconds: 3600,
      idle_timeout_seconds: 900,
    });
    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard/sessions/s-1"));
  });

  it("falls back to typing the Kit name when there is no list to choose from", async () => {
    listKits.mockResolvedValue([kit("upscale", "Image upscaling", "1.0.0", "batch")]);
    renderPage();
    await waitFor(() => expect(screen.getByText(/Type the Kit name/)).toBeTruthy());
    const input = screen.getByLabelText("Which Kit to run") as HTMLInputElement;
    expect(input.tagName).toBe("INPUT");
    expect(screen.getByRole("button", { name: "Start session" }).hasAttribute("disabled")).toBe(true);
    fireEvent.change(input, { target: { value: "notebooks@1.0.0" } });
    expect(screen.getByRole("button", { name: "Start session" }).hasAttribute("disabled")).toBe(false);
  });

  it("explains each limit in a sentence", async () => {
    renderPage();
    expect(await screen.findByText(/give back what you do not use/)).toBeTruthy();
    expect(screen.getByText(/always stops after this long/)).toBeTruthy();
    expect(screen.getByText(/do nothing for this long/)).toBeTruthy();
  });
});
