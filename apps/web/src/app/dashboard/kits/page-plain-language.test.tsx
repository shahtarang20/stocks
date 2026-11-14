// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/dashboard/kits",
}));

const listKits = vi.fn();
vi.mock("@/lib/kits", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kits")>();
  return { ...actual, kitsApi: { ...actual.kitsApi, listKits: (...a: unknown[]) => listKits(...a) } };
});

import KitCataloguePage from "./page";
import { kitBlurb } from "@/lib/kit-blurbs";

const kit = (slug: string, name: string, category: string, mode = "batch") => ({
  slug,
  name,
  category,
  version: "1.0.0",
  status: "published",
  image_digest: "sha256:" + "a".repeat(64),
  mode,
});

const KITS = [
  kit("upscale", "Image upscaling (Real-ESRGAN)", "enhancement"),
  kit("whisper-transcribe", "Speech to text (Whisper)", "audio"),
  kit("notebooks", "JupyterLab notebooks", "other", "session"),
  kit("llm-serve", "LLM serving (vLLM)", "inference", "service"),
  kit("brand-new-kit", "A brand new Kit", "science"),
];

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <KitCataloguePage />
    </QueryClientProvider>,
  );
}

beforeEach(() => listKits.mockResolvedValue(KITS));
afterEach(() => {
  cleanup();
  listKits.mockReset();
});

describe("Kit catalogue", () => {
  it("explains each Kit in plain words: what it does, what you give it, what you get", async () => {
    renderPage();
    const card = (await screen.findByText("Image upscaling (Real-ESRGAN)")).closest("li") as HTMLElement;
    expect(within(card).getByText("Make pictures bigger and sharper.")).toBeTruthy();
    expect(within(card).getByText("A picture, or a .zip of pictures")).toBeTruthy();
    expect(within(card).getByText("Bigger, sharper pictures")).toBeTruthy();
    expect(within(card).getByText("Version 1.0.0")).toBeTruthy();
    // The raw code ("upscale v1.0.0") is no longer shown to the person.
    expect(screen.queryByText(/upscale v1\.0\.0/)).toBeNull();
  });

  it("invents nothing for a Kit it has no description for", async () => {
    renderPage();
    const card = (await screen.findByText("A brand new Kit")).closest("li") as HTMLElement;
    expect(kitBlurb("brand-new-kit")).toBeNull();
    expect(within(card).queryByText(/You give it/)).toBeNull();
    expect(within(card).getByRole("link", { name: /Start a job/ })).toBeTruthy();
  });

  it("keeps the right action for each kind of Kit", async () => {
    renderPage();
    const notebook = (await screen.findByText("JupyterLab notebooks")).closest("li") as HTMLElement;
    expect(within(notebook).getByRole("link", { name: /Start a session/ })).toBeTruthy();
    const service = screen.getByText("LLM serving (vLLM)").closest("li") as HTMLElement;
    expect(within(service).getByText(/always-on service/)).toBeTruthy();
    expect(within(service).queryByRole("link")).toBeNull();
  });

  it("finds Kits by what they do, not only by their names", async () => {
    renderPage();
    await screen.findByText("Image upscaling (Real-ESRGAN)");
    fireEvent.change(screen.getByLabelText("Search Kits"), { target: { value: "speech" } });
    expect(screen.getByText("Speech to text (Whisper)")).toBeTruthy();
    expect(screen.queryByText("Image upscaling (Real-ESRGAN)")).toBeNull();

    // "sharper" appears only in the plain description of the upscaling Kit.
    fireEvent.change(screen.getByLabelText("Search Kits"), { target: { value: "sharper pictures" } });
    expect(screen.getByText("Image upscaling (Real-ESRGAN)")).toBeTruthy();
    expect(screen.queryByText("Speech to text (Whisper)")).toBeNull();
  });

  it("says so when nothing matches, and lets the person start again", async () => {
    renderPage();
    await screen.findByText("Image upscaling (Real-ESRGAN)");
    fireEvent.change(screen.getByLabelText("Search Kits"), { target: { value: "zzzzz" } });
    expect(await screen.findByText("No Kits match that")).toBeTruthy();
    expect(screen.queryByText("No Kits are ready yet")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show all Kits" }));
    expect(screen.getByText("Image upscaling (Real-ESRGAN)")).toBeTruthy();
    expect((screen.getByLabelText("Search Kits") as HTMLInputElement).value).toBe("");
  });

  it("keeps saying 'No Kits are ready yet' when there really are none", async () => {
    listKits.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText("No Kits are ready yet")).toBeTruthy();
  });
});
