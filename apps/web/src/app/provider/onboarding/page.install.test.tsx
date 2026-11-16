// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({ usePathname: () => "/provider/onboarding", useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: null, user: null, isAuthenticated: true }) }));

import OnboardingPage from "./page";

beforeEach(() => {
  cleanup();
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OnboardingPage />
    </QueryClientProvider>,
  );
}

describe("host setup page: one-line install", () => {
  it("shows one copyable line that has no token in it", () => {
    renderPage();
    expect(screen.getByText("Install and connect (Linux)")).toBeTruthy();
    const lines = screen.getAllByText(/curl -fsSL .*install\.sh/).map((e) => e.textContent ?? "");
    const pasted = lines.find((t) => t.includes("--server") && !t.includes("--token") && t.includes("mktemp")) ?? "";
    expect(pasted).toMatch(/^f=\$\(mktemp\) && curl -fsSL https?:\/\/[^ ]+\/install\.sh -o "\$f" && sudo sh "\$f" --server https?:\/\/\S+; rm -f "\$f"$/);
  });

  it("shows the token separately, to paste when the installer asks", () => {
    renderPage();
    expect(screen.getByText("Your setup token")).toBeTruthy();
    expect(screen.getByText(/It will ask for your setup token \(your typing is hidden\)/)).toBeTruthy();
  });

  it("keeps a no-prompt version for scripts and explains its cost", () => {
    renderPage();
    expect(screen.getByText("Install without a prompt (for scripts)")).toBeTruthy();
    const unattended = screen.getAllByText(/^curl -fsSL .*--token PASTE_YOUR_TOKEN_HERE/);
    expect(unattended.length).toBeGreaterThan(0);
    expect(screen.getByText(/other people using that computer could read it/)).toBeTruthy();
  });

  it("says so plainly when the token is only a placeholder", () => {
    renderPage();
    expect(screen.getByText(/Create a token in step 3b first/)).toBeTruthy();
  });

  it("no longer sends a host to 'the team' for the normal path", () => {
    renderPage();
    expect(screen.getByText(/You can skip this step/)).toBeTruthy();
    expect(screen.getByText("Install by hand (advanced)")).toBeTruthy();
    expect(screen.getByText("Connect by hand (if you installed it yourself)")).toBeTruthy();
  });

  it("explains what the line does and how to remove it", () => {
    renderPage();
    expect(screen.getByText(/keep running after a restart/)).toBeTruthy();
    expect(screen.getByText(/--uninstall/)).toBeTruthy();
  });
});

describe("host setup page: the optional rental tick-box", () => {
  const pasted = () =>
    screen.getAllByText(/curl -fsSL .*install\.sh/).map((e) => e.textContent ?? "").find((t) => t.includes("mktemp")) ?? "";

  it("is unticked at first, and the line has no rental word", () => {
    renderPage();
    const box = screen.getByRole("checkbox", { name: /Also let customers run their own code/ }) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(pasted()).not.toContain("--allow-rental");
  });

  it("adds the rental word to the line when ticked, and removes it when unticked", () => {
    renderPage();
    const box = screen.getByRole("checkbox", { name: /Also let customers run their own code/ });
    fireEvent.click(box);
    expect(pasted()).toMatch(/--server \S+ --allow-rental; rm -f "\$f"$/);
    expect(pasted()).not.toContain("--token");
    fireEvent.click(box);
    expect(pasted()).not.toContain("--allow-rental");
  });

  it("says plainly what ticking it does", () => {
    renderPage();
    expect(screen.getByText(/also turns container rental on and tests the\s+computer/)).toBeTruthy();
  });
});

