// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({ usePathname: () => "/provider/onboarding", useRouter: () => ({ push: vi.fn() }) }));
const auth = { accessToken: "t", refreshUser: vi.fn(), user: null, isAuthenticated: true };
vi.mock("@/lib/auth-context", () => ({ useAuth: () => auth }));

import OnboardingPage from "./page";

const TOKEN = "enr_Ab1-_cdEFgh2345ijKLmnOP6789qrSTuvWXyz0123456";
const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/provider/enrollment-tokens") && init?.method === "POST") {
      return json({ enrollment_token_id: "id1", enrollment_token: TOKEN, expires_at: new Date(Date.now() + 3600_000).toISOString(), label: null }, 201);
    }
    if (url.includes("/provider/enrollment-tokens")) return json([]);
    if (url.includes("/provider/machines")) return json([]);
    return new Promise(() => {});
  }));
});
afterEach(() => cleanup());

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OnboardingPage />
    </QueryClientProvider>,
  );
}

describe("host setup page: a new setup token", () => {
  it("replaces the placeholder in step 3c as soon as the token is created", async () => {
    renderPage();
    expect(screen.getByText(/Create a token in step 3b first/)).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /Create setup token/ }));
    await waitFor(() => expect(screen.queryByText(/Create a token in step 3b first/)).toBeNull());
    expect(document.body.textContent).toContain(TOKEN);
    expect(screen.queryByText(/^curl -fsSL .*--token PASTE_YOUR_TOKEN_HERE/)).toBeNull();
    expect(screen.getAllByText(new RegExp(`--token ${TOKEN}`)).length).toBeGreaterThan(0);
  });
});

describe("host setup page: the one-click button", () => {
  const writeText = vi.fn((_text: string) => Promise.resolve());
  beforeEach(() => {
    writeText.mockClear();
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  });


  const pressOneClick = async () => {
    const name = "Create token and copy the install command";
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name }));
  };

  it("creates a token and copies one finished line that carries it", async () => {
    renderPage();
    await pressOneClick();
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = String(writeText.mock.calls[0]?.[0]);
    expect(copied).toMatch(new RegExp(`^export COMPUTECOIN_ENROLL_TOKEN='${TOKEN}'; f=\\$\\(mktemp\\) && curl -fsSL `));
    expect(copied).toContain("sudo --preserve-env=COMPUTECOIN_ENROLL_TOKEN sh");
    expect(copied).toMatch(/unset COMPUTECOIN_ENROLL_TOKEN$/);
    expect(await screen.findByText(/Copied\. Paste it in the terminal now/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy the install command again" })).toBeTruthy();
  });

  it("includes the rental word when the rental box is ticked", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("checkbox", { name: /Also let customers run their own code/ }));
    await pressOneClick();
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String(writeText.mock.calls[0]?.[0])).toMatch(/--server \S+ --allow-rental; rm -f "\$f"; unset/);
  });

  it("says to press the copy button when the browser refuses to copy", async () => {
    writeText.mockImplementationOnce(() => Promise.reject(new Error("blocked")));
    renderPage();
    await pressOneClick();
    expect(await screen.findByText(/Your browser did not let us copy it/)).toBeTruthy();
    expect(document.body.textContent).toContain(`export COMPUTECOIN_ENROLL_TOKEN='${TOKEN}'`); // still shown, so it can be copied by hand
  });

  it("shows no finished line until a token exists", async () => {
    renderPage();
    await screen.findByRole("button", { name: "Create token and copy the install command" });
    expect(document.body.textContent).not.toContain("export COMPUTECOIN_ENROLL_TOKEN");
  });
});

