// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const push = vi.fn();
const registerUser = vi.fn();
const loginUser = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/login",
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ login: loginUser, register: registerUser, refreshUser: vi.fn() }),
}));

import { AuthForm } from "./auth-form";

function fill(email: string, password: string) {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
}

beforeEach(() => {
  push.mockReset();
  registerUser.mockReset().mockResolvedValue(undefined);
  loginUser.mockReset().mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe("AuthForm password field", () => {
  it("lets a person show and hide what they typed, so a typo is easy to spot", () => {
    render(<AuthForm mode="login" />);
    const input = screen.getByLabelText("Password") as HTMLInputElement;
    expect(input.type).toBe("password");

    const show = screen.getByRole("button", { name: "Show password" });
    expect(show.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(show);
    expect(input.type).toBe("text");

    const hide = screen.getByRole("button", { name: "Hide password" });
    expect(hide.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(hide);
    expect(input.type).toBe("password");
  });

  it("does not submit the form when the show/hide button is pressed", () => {
    render(<AuthForm mode="login" />);
    fill("dev@example.com", "secret");
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(loginUser).not.toHaveBeenCalled();
  });
});

describe("AuthForm: the 8-character rule is for choosing a password, not for logging in", () => {
  it("lets an existing account with a shorter password log in", async () => {
    render(<AuthForm mode="login" />);
    fill("old@example.com", "short1");
    fireEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    await waitFor(() => expect(loginUser).toHaveBeenCalledWith("old@example.com", "short1"));
  });

  it("still asks for a password when logging in", async () => {
    render(<AuthForm mode="login" />);
    fill("dev@example.com", "");
    fireEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    expect(await screen.findByText("Enter your password.")).toBeTruthy();
    expect(loginUser).not.toHaveBeenCalled();
  });

  it("still refuses a short password when creating an account", async () => {
    render(<AuthForm mode="register" />);
    fill("new@example.com", "short1");
    fireEvent.click(screen.getByLabelText(/Acceptable Use Policy/i));
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    expect(await screen.findByText(/too short. Use at least 8 characters/)).toBeTruthy();
    expect(registerUser).not.toHaveBeenCalled();
  });
});
