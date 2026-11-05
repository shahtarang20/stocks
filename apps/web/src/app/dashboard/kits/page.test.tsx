// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/dashboard/kits" }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "tok" }) }));

const kits = [
  { slug: "render", version: "1.0.0", name: "Render", category: "3d", status: "published", image_digest: "x", mode: "batch" },
  { slug: "term", version: "2.0.0", name: "Terminal", category: "other", status: "published", image_digest: "x", mode: "session" },
  { slug: "legacy", version: "1.0.0", name: "Legacy", category: "other", status: "published", image_digest: "x" }, // older server: no mode
];
vi.mock("@/lib/kits", async (orig) => {
  const real = await orig<typeof import("@/lib/kits")>();
  return { ...real, kitsApi: { ...real.kitsApi, listKits: () => Promise.resolve(kits) } };
});

import KitCataloguePage from "./page";

afterEach(() => cleanup());

describe("Kit catalogue", () => {
  it("starts batch Kits as jobs and session Kits from Sessions", async () => {
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><KitCataloguePage /></QueryClientProvider>);
    await screen.findByText("Render");
    const hrefs = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs).toContain("/dashboard/kits/render?v=1.0.0");
    expect(hrefs).toContain("/dashboard/kits/legacy?v=1.0.0");
    expect(hrefs).toContain("/dashboard/sessions?kit=term%402.0.0");
    expect(hrefs).not.toContain("/dashboard/kits/term?v=2.0.0");
  });
});
