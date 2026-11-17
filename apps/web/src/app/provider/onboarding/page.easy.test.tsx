// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({ usePathname: () => "/provider/onboarding", useRouter: () => ({ push: vi.fn() }) }));
const auth = { accessToken: "t", refreshUser: vi.fn(), user: null, isAuthenticated: true };
vi.mock("@/lib/auth-context", () => ({ useAuth: () => auth }));

import OnboardingPage from "./page";

const TOKEN = "enr_FAKE-TEST_FAKE-TEST_FAKE-TEST_FAKE-TEST"; // gitleaks:allow
const KEY = "computecoin.host-setup";
const MSI = "https://github.com/acme/stocks/releases/download/agent-v0.2.1/ComputeCoinAgent-0.2.1.msi";
const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
const machine = (over: Record<string, unknown> = {}) => ({
  machine_id: "m1", hostname: "gaming-pc", agent_version: "0.2.0", status: "online", availability: "online", asking_price_per_hour: "1.00", capabilities: [], ...over,
});

let tokenList: unknown[] = [];
let machineList: unknown[] = [];
let registerCalls = 0;
const writeText = vi.fn((_t: string) => Promise.resolve());

beforeEach(() => {
  window.localStorage.clear();
  tokenList = [];
  machineList = [];
  registerCalls = 0;
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/provider/register")) {
      registerCalls += 1;
      return json({ ok: true });
    }
    if (url.includes("/provider/enrollment-tokens") && init?.method === "POST") {
      return json({ enrollment_token_id: "id1", enrollment_token: TOKEN, expires_at: new Date(Date.now() + 3600_000).toISOString(), label: null }, 201);
    }
    if (url.includes("/provider/enrollment-tokens")) return json(tokenList);
    if (url.includes("/provider/machines")) return json(machineList);
    return new Promise(() => {});
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OnboardingPage />
    </QueryClientProvider>,
  );
}
const seed = (persona: string, mode: string) => window.localStorage.setItem(KEY, JSON.stringify({ persona, mode }));
const stepOf = (id: string) => document.querySelector(`[data-step="${id}"]`) as HTMLElement;
const status = (id: string) => stepOf(id).getAttribute("data-status");

describe("host setup page: choosing your situation", () => {
  it("asks first, with three cards, and shows no steps or commands until a card is picked", () => {
    renderPage();
    expect(screen.getByTestId("persona-cards")).toBeTruthy();
    expect(screen.getAllByRole("button").filter((b) => /I have one computer|I have a server|I run many machines/.test(b.textContent ?? "")).length).toBe(3);
    expect(screen.queryByTestId("host-wizard")).toBeNull();
    expect(screen.queryByText("Install the agent")).toBeNull();
  });

  it("'one computer' opens the guided steps and remembers it", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /I have one computer/ }));
    expect(screen.getByTestId("host-wizard")).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem(KEY) as string)).toEqual({ persona: "one_pc", mode: "easy" });
  });

  it.each(["server", "many"])("'%s' opens the full page with every command", (persona) => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: persona === "server" ? /I have a server/ : /I run many machines/ }));
    expect(screen.queryByTestId("host-wizard")).toBeNull();
    expect(screen.getByText("Install the agent")).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem(KEY) as string)).toEqual({ persona, mode: "advanced" });
  });

  it("opens straight into what was chosen last time, and 'Change my choice' goes back to the cards", () => {
    seed("one_pc", "easy");
    renderPage();
    expect(screen.getByTestId("host-wizard")).toBeTruthy();
    expect(screen.queryByTestId("persona-cards")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Change my choice" }));
    expect(screen.getByTestId("persona-cards")).toBeTruthy();
    expect(screen.queryByTestId("host-wizard")).toBeNull();
  });

  it("switches between Easy and Advanced and keeps the switch", () => {
    seed("one_pc", "easy");
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Show every step and command" }));
    expect(screen.getByText("Install the agent")).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem(KEY) as string)).toEqual({ persona: "one_pc", mode: "advanced" });
    fireEvent.click(screen.getByRole("button", { name: "Show one step at a time" }));
    expect(screen.getByTestId("host-wizard")).toBeTruthy();
  });

  it("ignores a saved value it does not know and just asks", () => {
    window.localStorage.setItem(KEY, '{"persona":"admin","mode":"easy"}');
    renderPage();
    expect(screen.getByTestId("persona-cards")).toBeTruthy();
  });
});

describe("host setup page: the guided steps tick themselves", () => {
  beforeEach(() => seed("one_pc", "easy"));

  it("starts at 'Turn on hosting' for an account that is not a host yet", async () => {
    // not a host until the register call has been made: listing tokens is refused before that
    (fetch as ReturnType<typeof vi.fn>).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/provider/register")) {
        registerCalls += 1;
        return json({ ok: true });
      }
      if (url.includes("/provider/enrollment-tokens")) return registerCalls > 0 ? json([]) : json({ detail: "not a host" }, 403);
      if (url.includes("/provider/machines")) return json([]);
      return new Promise(() => {});
    });
    renderPage();
    await waitFor(() => expect(status("hosting")).toBe("current"));
    expect(status("computer")).toBe("done");
    expect(status("install")).toBe("upcoming");
    expect(screen.queryByRole("button", { name: "Create token and copy the install command" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Become a host" }));
    await waitFor(() => expect(registerCalls).toBe(1));
    await waitFor(() => expect(status("hosting")).toBe("done"));
    expect(status("install")).toBe("current");
  });

  it("is on 'Install' for a host with no computer, and the one button creates a token and copies the line", async () => {
    renderPage();
    await waitFor(() => expect(status("install")).toBe("current"));
    const name = "Create token and copy the install command";
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String(writeText.mock.calls[0]?.[0])).toContain(`export COMPUTECOIN_ENROLL_TOKEN='${TOKEN}'`);
  });

  it("asks nothing about container rental unless it is ticked, then adds the word", async () => {
    renderPage();
    const box = await screen.findByRole("checkbox", { name: /Also let customers run their own code/ });
    expect((box as HTMLInputElement).checked).toBe(false);
    fireEvent.click(box);
    const name = "Create token and copy the install command";
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String(writeText.mock.calls[0]?.[0])).toContain("--allow-rental");
  });

  it("moves to 'Set your price' once a computer has connected", async () => {
    machineList = [machine({ asking_price_per_hour: null, availability: "offline", status: "offline" })];
    renderPage();
    await waitFor(() => expect(status("install")).toBe("done"));
    expect(status("price")).toBe("current");
    expect(within(stepOf("price")).getByRole("link", { name: "Open my computers" }).getAttribute("href")).toBe("/provider/hardware");
    expect(screen.queryByRole("button", { name: "Create token and copy the install command" })).toBeNull();
  });

  it("is all ticked when a priced, Online computer is reporting in", async () => {
    machineList = [machine()];
    renderPage();
    await waitFor(() => expect(status("live")).toBe("done"));
    for (const id of ["computer", "hosting", "install", "price", "live"]) expect(status(id)).toBe("done");
    expect(within(stepOf("live")).getByRole("link", { name: "See my earnings" })).toBeTruthy();
  });

  it("says nothing about container rental unless the box was ticked", async () => {
    machineList = [machine({ capabilities: ["container_tier", "container_rental"] })];
    renderPage();
    await waitFor(() => expect(status("live")).toBe("done"));
    expect(screen.queryByText(/Container rental is/)).toBeNull();
  });

  it("says container rental is on when it was ticked and the computer reports it", async () => {
    machineList = [machine({ capabilities: ["container_tier", "container_rental"] })];
    renderPage();
    // a computer is already connected, so the install step (and its box) is done: tick it in Advanced, then come back
    fireEvent.click(screen.getByRole("button", { name: "Show every step and command" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /Also let customers run their own code/ }));
    fireEvent.click(screen.getByRole("button", { name: "Show one step at a time" }));
    await waitFor(() => expect(status("live")).toBe("done"));
    expect(within(stepOf("live")).getByText("Container rental is on for this computer.")).toBeTruthy();
  });
});

describe("host setup page: choosing the kind of computer", () => {
  beforeEach(() => seed("one_pc", "easy"));

  it("preselects from the browser (the test browser says Linux) and can be changed", async () => {
    renderPage();
    await waitFor(() => expect(status("install")).toBe("current"));
    expect(screen.getByRole("button", { name: "Linux" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(/looks like it is on a Linux computer/)).toBeTruthy();
  });

  it("preselects Windows when the browser is on Windows", async () => {
    vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36");
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: /Windows 11/ }).getAttribute("aria-pressed")).toBe("true"));
  });

  it("calls a Mac unsupported and blocks the first step until Linux or Windows is picked", async () => {
    vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15");
    renderPage();
    await waitFor(() => expect(status("computer")).toBe("blocked"));
    expect(screen.getByText(/A Mac cannot share a graphics card/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Linux" }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.queryByRole("button", { name: "Create token and copy the install command" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Linux" }));
    await waitFor(() => expect(status("computer")).toBe("done"));
  });

  it("on Windows without a published installer, blocks the install step with the reason and offers no command", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Windows 11/ }));
    await waitFor(() => expect(status("install")).toBe("blocked"));
    expect(within(stepOf("install")).getByText(/installer is not published yet/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/Invoke-WebRequest/);
    expect(screen.queryByRole("button", { name: "Create token and copy the install command" })).toBeNull();
  });

  it("on Windows with a published installer, the button waits for 'I have read this' and then copies the PowerShell block with the token", async () => {
    vi.stubEnv("NEXT_PUBLIC_WINDOWS_AGENT_MSI_URL", MSI);
    vi.stubEnv("NEXT_PUBLIC_WINDOWS_AGENT_MSI_SHA256", "b".repeat(64));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Windows 11/ }));
    const name = "Create token and copy the install command";
    const button = await screen.findByRole("button", { name });
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true));
    expect(screen.getByText(/Tick "I have read this" above first/)).toBeTruthy();
    fireEvent.click(button); // disabled: nothing happens
    expect(writeText).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = String(writeText.mock.calls[0]?.[0]);
    expect(copied).toContain(`Invoke-WebRequest -Uri '${MSI}'`);
    expect(copied).toContain(`-Value '${TOKEN}'`);
    expect(copied).toContain("--token-file $tf");
    expect(copied).not.toContain("rental on"); // rental was not ticked
  });

  it("puts the rental commands in the Windows block only when that box is ticked", async () => {
    vi.stubEnv("NEXT_PUBLIC_WINDOWS_AGENT_MSI_URL", MSI);
    vi.stubEnv("NEXT_PUBLIC_WINDOWS_AGENT_MSI_SHA256", "b".repeat(64));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Windows 11/ }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /Also let customers run their own code/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    const name = "Create token and copy the install command";
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String(writeText.mock.calls[0]?.[0])).toContain("computecoin_agent rental on");
  });
});

describe("host setup page: Advanced mode has the Windows one-click button too", () => {
  it("shows it once Windows is chosen and the installer is published, and it waits for the consent tick", async () => {
    vi.stubEnv("NEXT_PUBLIC_WINDOWS_AGENT_MSI_URL", MSI);
    vi.stubEnv("NEXT_PUBLIC_WINDOWS_AGENT_MSI_SHA256", "b".repeat(64));
    seed("server", "advanced");
    renderPage();
    fireEvent.click(screen.getByRole("radio", { name: /Windows 11 \(experimental\)/ }));
    const name = "Create token and copy the install command";
    const button = await screen.findByRole("button", { name });
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    await waitFor(() => expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String(writeText.mock.calls[0]?.[0])).toContain(`-Value '${TOKEN}'`);
  });

  it("shows no button at all on Windows while the installer is not published", () => {
    seed("server", "advanced");
    renderPage();
    fireEvent.click(screen.getByRole("radio", { name: /Windows 11 \(experimental\)/ }));
    expect(screen.queryByRole("button", { name: "Create token and copy the install command" })).toBeNull();
  });
});
