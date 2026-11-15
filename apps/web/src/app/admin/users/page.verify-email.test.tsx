// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "tok", email: "admin@example.com" }) }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("@/components/console/toast", () => ({ useToast: () => toast }));

const api = vi.hoisted(() => ({
  listAdminUsers: vi.fn(),
  verifyUserEmail: vi.fn(),
  suspendUser: vi.fn(),
  reinstateUser: vi.fn(),
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api }));

import AdminUsersPage from "./page";

const user = (over = {}) => ({
  user_id: "u1", email: "friend@example.com", status: "active", roles: ["customer", "provider"],
  email_verified: false, created_at: "2026-11-14T10:00:00Z", ...over,
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><AdminUsersPage /></QueryClientProvider>);
}

beforeEach(() => {
  cleanup();
  Object.values(api).forEach((f) => f.mockReset());
  Object.values(toast).forEach((f) => f.mockReset());
  api.listAdminUsers.mockResolvedValue([user()]);
  api.verifyUserEmail.mockResolvedValue({ user_id: "u1", email: "friend@example.com", status: "active", message: "The email has been marked as confirmed." });
});

describe("admin users: confirm an email by hand", () => {
  it("offers the button only for an active or suspended account whose email is not confirmed", async () => {
    api.listAdminUsers.mockResolvedValue([
      user({ user_id: "a", email: "unconfirmed@example.com" }),
      user({ user_id: "b", email: "confirmed@example.com", email_verified: true }),
      user({ user_id: "c", email: "gone@example.com", status: "deleted" }),
      user({ user_id: "d", email: "suspended@example.com", status: "suspended" }),
    ]);
    renderPage();
    await screen.findByText("unconfirmed@example.com");
    expect(screen.getAllByRole("button", { name: /Confirm email/ })).toHaveLength(2); // unconfirmed and suspended-unconfirmed only
  });

  it("asks for a reason of at least five characters before it will do anything", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Confirm email/ }));
    const save = screen.getByRole("button", { name: "Mark email as confirmed" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    const box = screen.getByLabelText(/Why\?/);
    fireEvent.change(box, { target: { value: "no" } });
    expect(save.disabled).toBe(true);
    fireEvent.change(box, { target: { value: "    ab    " } }); // spaces do not count
    expect(save.disabled).toBe(true);
    fireEvent.change(box, { target: { value: "Known host" } });
    expect(save.disabled).toBe(false);
    expect(api.verifyUserEmail).not.toHaveBeenCalled();
  });

  it("warns what confirming unlocks, and says it is kept in the audit log", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Confirm email/ }));
    const dialog = screen.getByRole("dialog").textContent ?? "";
    expect(dialog).toMatch(/billing, the wallet, withdrawals and running jobs/);
    expect(dialog).toMatch(/only do it for someone you know/);
    expect(dialog).toMatch(/saved in the audit log/);
  });

  it("sends the trimmed reason for that user and tells the admin it worked", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Confirm email/ }));
    fireEvent.change(screen.getByLabelText(/Why\?/), { target: { value: "   Known host, no email yet  " } });
    fireEvent.click(screen.getByRole("button", { name: "Mark email as confirmed" }));
    await waitFor(() => expect(api.verifyUserEmail).toHaveBeenCalledWith("tok", "u1", "Known host, no email yet"));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("The email has been marked as confirmed."));
    await waitFor(() => expect(screen.queryByLabelText(/Why\?/)).toBeNull()); // the dialog closed
    expect(api.listAdminUsers.mock.calls.length).toBeGreaterThan(1); // and the list was reloaded
  });

  it("shows the server's reason when it is refused, and keeps the dialog open", async () => {
    const { ApiError } = await import("@/lib/api");
    api.verifyUserEmail.mockRejectedValue(new ApiError("account_deleted", 409));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Confirm email/ }));
    fireEvent.change(screen.getByLabelText(/Why\?/), { target: { value: "Known host" } });
    fireEvent.click(screen.getByRole("button", { name: "Mark email as confirmed" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/account_deleted/);
    expect(screen.getByLabelText(/Why\?/)).toBeTruthy();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("cancel closes it without sending anything", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Confirm email/ }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByLabelText(/Why\?/)).toBeNull());
    expect(api.verifyUserEmail).not.toHaveBeenCalled();
  });

  it("a second admin opening it later starts with an empty reason", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Confirm email/ }));
    fireEvent.change(screen.getByLabelText(/Why\?/), { target: { value: "Known host" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(await screen.findByRole("button", { name: /Confirm email/ }));
    expect((screen.getByLabelText(/Why\?/) as HTMLTextAreaElement).value).toBe("");
  });

  it("does not remove the suspend and reinstate buttons", async () => {
    renderPage();
    await screen.findByText("friend@example.com");
    expect(screen.getByRole("button", { name: /Suspend account/ })).toBeTruthy();
  });
});
