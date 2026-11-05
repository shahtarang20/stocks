// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

let auth: { user: { roles: string[] } | null; isAdmin: boolean } = { user: null, isAdmin: false };
vi.mock("@/lib/auth-context", () => ({ useAuth: () => auth }));

import { AdminGate } from "./admin-gate";

afterEach(() => cleanup());

describe("AdminGate", () => {
  it("shows the admin page to administrators", () => {
    auth = { user: { roles: ["administrator"] }, isAdmin: true };
    render(<AdminGate><p>secret table</p></AdminGate>);
    expect(screen.getByText("secret table")).toBeTruthy();
  });
  it("blocks a signed-in non-admin with a clear message and a way back", () => {
    auth = { user: { roles: ["customer"] }, isAdmin: false };
    render(<AdminGate><p>secret table</p></AdminGate>);
    expect(screen.queryByText("secret table")).toBeNull();
    expect(screen.getByText("Administrators only")).toBeTruthy();
    expect(screen.getByRole("link", { name: /back to your console/i }).getAttribute("href")).toBe("/dashboard");
  });
  it("leaves loading and redirect handling to AuthGuard while there is no user", () => {
    auth = { user: null, isAdmin: false };
    render(<AdminGate><p>child</p></AdminGate>);
    expect(screen.getByText("child")).toBeTruthy();
  });
});
