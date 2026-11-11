// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/dashboard/sessions",
}));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ isReady: true, isAuthenticated: false }),
}));

import { AuthGuard } from "./auth-guard";

afterEach(() => {
  cleanup();
  replace.mockReset();
  window.history.replaceState(null, "", "/");
});

describe("AuthGuard", () => {
  it("sends a signed-out visitor to /login and remembers the page including its query string", () => {
    window.history.replaceState(null, "", "/dashboard/sessions?kit=terminal-gpu@1.0.0");
    render(<AuthGuard>secret</AuthGuard>);
    expect(replace).toHaveBeenCalledWith(
      `/login?next=${encodeURIComponent("/dashboard/sessions?kit=terminal-gpu@1.0.0")}`,
    );
  });
});
