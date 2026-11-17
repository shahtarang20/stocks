// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { WindowsInstallPanel } from "./windows-install-panel";
import { WINDOWS_CONSENT_POINTS, WINDOWS_INSTALL_NEEDS, WINDOWS_RENTAL_CONSENT_POINTS } from "@/lib/windows-install-command";

afterEach(() => cleanup());

const URL_OK = "https://github.com/acme/stocks/releases/download/agent-v0.2.1/ComputeCoinAgent-0.2.1.msi";
const SHA = "a".repeat(64);
const TOKEN = "enr_FAKE-TEST_FAKE-TEST_FAKE-TEST_FAKE-TEST"; // gitleaks:allow

function panel(over: Partial<React.ComponentProps<typeof WindowsInstallPanel>> = {}) {
  const calls: boolean[] = [];
  const utils = render(
    <WindowsInstallPanel msiUrl={URL_OK} sha256={SHA} server="https://api.example.com" allowRental={false} onAllowRentalChange={(v) => calls.push(v)} {...over} />,
  );
  return { ...utils, calls };
}

const text = () => document.body.textContent ?? "";

describe("WindowsInstallPanel", () => {
  it("says plainly that it is experimental and lists what the PC needs", () => {
    panel();
    expect(text()).toMatch(/Experimental\./);
    expect(text()).toMatch(/Linux is the recommended choice/);
    for (const n of WINDOWS_INSTALL_NEEDS) expect(screen.getByText(n)).toBeTruthy();
  });

  it("offers nothing to copy while the installer is not published, and says so", () => {
    for (const missing of [{ msiUrl: undefined }, { sha256: undefined }, { msiUrl: "http://x.com/a.msi" }, { sha256: "abc" }]) {
      cleanup();
      panel(missing);
      expect(screen.getByTestId("windows-not-published").textContent).toMatch(/has not been published yet/);
      expect(screen.queryByRole("checkbox")).toBeNull();
      expect(text()).not.toMatch(/Invoke-WebRequest|msiexec/);
    }
  });

  it("shows what the installer does, and no command until the person has ticked that they read it", () => {
    panel();
    for (const p of WINDOWS_CONSENT_POINTS) expect(screen.getByText(p)).toBeTruthy();
    expect(text()).toMatch(/Tick the box above to see the command/);
    expect(text()).not.toMatch(/Invoke-WebRequest|msiexec/);
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    expect(text()).toMatch(/Invoke-WebRequest/);
    expect(text()).toContain(URL_OK);
    expect(text()).toContain(`-ne '${SHA}'`);
    expect(text()).toContain("LicenseAccepted=1");
  });

  it("asks for the token with typing hidden, and shows a second block with the token only when one was created", () => {
    const { unmount } = panel();
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    expect(text()).toMatch(/Read-Host 'Paste your setup token/);
    expect(text()).not.toContain(TOKEN);
    expect(screen.queryByText(/already in it/)).toBeNull();
    unmount();
    panel({ token: TOKEN });
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    expect(screen.getByText(/with your token already in it/)).toBeTruthy();
    expect(text()).toContain(TOKEN);
  });

  it("adds the rental commands and the rental consent points only when the rental box is ticked", () => {
    const { rerender, calls } = panel();
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    expect(text()).not.toMatch(/computecoin_agent rental/);
    expect(screen.queryAllByTestId("windows-rental-point")).toHaveLength(0);
    fireEvent.click(screen.getByRole("checkbox", { name: /Also let customers run their own code/ }));
    expect(calls).toEqual([true]);
    rerender(<WindowsInstallPanel msiUrl={URL_OK} sha256={SHA} server="https://api.example.com" allowRental={true} onAllowRentalChange={() => {}} />);
    expect(text()).toMatch(/computecoin_agent rental check/);
    expect(text()).toMatch(/computecoin_agent rental on/);
    expect(screen.getAllByTestId("windows-rental-point")).toHaveLength(WINDOWS_RENTAL_CONSENT_POINTS.length);
  });

  it("does not invent a command from a server address it cannot vouch for", () => {
    panel({ server: null });
    fireEvent.click(screen.getByRole("checkbox", { name: /I have read this/ }));
    expect(text()).toMatch(/could not be built from this website's address/);
    expect(text()).not.toMatch(/Invoke-WebRequest/);
  });
});
