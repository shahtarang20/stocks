// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const push = vi.fn();
const registerUser = vi.fn();
const loginUser = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/register",
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ login: loginUser, register: registerUser, refreshUser: vi.fn() }),
}));

import { AuthForm } from "./auth-form";

async function fill(email = "dev@example.com", password = "correct-horse") {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
}

beforeEach(() => {
  push.mockReset();
  registerUser.mockReset().mockResolvedValue(undefined);
  loginUser.mockReset().mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe("AuthForm AUP (sign-up)", () => {
  it("shows the unticked AUP checkbox on sign-up only", () => {
    render(<AuthForm mode="register" />);
    expect((screen.getByLabelText(/Acceptable Use Policy/i) as HTMLInputElement).checked).toBe(false);
    cleanup();
    render(<AuthForm mode="login" />);
    expect(screen.queryByLabelText(/Acceptable Use Policy/i)).toBeNull();
  });

  it("blocks sign-up until the AUP is ticked and shows an error", async () => {
    render(<AuthForm mode="register" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    expect(await screen.findByText(/need to accept the Acceptable Use Policy/i)).toBeTruthy();
    expect(registerUser).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("clears the error when ticked, then sends accept_aup and aup_version", async () => {
    render(<AuthForm mode="register" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    await screen.findByText(/need to accept the Acceptable Use Policy/i);
    fireEvent.click(screen.getByLabelText(/Acceptable Use Policy/i));
    expect(screen.queryByText(/need to accept the Acceptable Use Policy/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    await waitFor(() => expect(registerUser).toHaveBeenCalledTimes(1));
    const [email, password, aup] = registerUser.mock.calls[0]!;
    expect(email).toBe("dev@example.com");
    expect(password).toBe("correct-horse");
    expect(aup).toEqual({ accept_aup: true, aup_version: expect.any(String) });
    expect(aup.aup_version.length).toBeGreaterThan(0);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
  });

  it("does not ask for the AUP when logging in", async () => {
    render(<AuthForm mode="login" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    await waitFor(() => expect(loginUser).toHaveBeenCalledWith("dev@example.com", "correct-horse"));
    expect(registerUser).not.toHaveBeenCalled();
  });
});
