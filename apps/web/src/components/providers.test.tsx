// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

let currentUser: { id: string } | null = null;
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ user: currentUser }),
  AuthProvider: ({ children }: { children: unknown }) => children,
}));

import { AuthCacheScope } from "./providers";

afterEach(() => {
  cleanup();
  currentUser = null;
});

function mount(qc: QueryClient) {
  return render(
    <QueryClientProvider client={qc}>
      <AuthCacheScope />
    </QueryClientProvider>,
  );
}

describe("AuthCacheScope", () => {
  it("drops the cached queries when the signed-in account signs out or changes", () => {
    const qc = new QueryClient();
    currentUser = null;
    const view = mount(qc);

    // first sign-in: nothing to drop, and a cache filled after sign-in survives re-renders
    currentUser = { id: "user-a" };
    view.rerender(<QueryClientProvider client={qc}><AuthCacheScope /></QueryClientProvider>);
    qc.setQueryData(["jobs"], [{ job_id: "a-secret-job" }]);
    view.rerender(<QueryClientProvider client={qc}><AuthCacheScope /></QueryClientProvider>);
    expect(qc.getQueryData(["jobs"])).toBeDefined();

    // sign out: user A's data must be gone before anyone else signs in
    currentUser = null;
    view.rerender(<QueryClientProvider client={qc}><AuthCacheScope /></QueryClientProvider>);
    expect(qc.getQueryData(["jobs"])).toBeUndefined();

    // a different account signs in
    currentUser = { id: "user-b" };
    view.rerender(<QueryClientProvider client={qc}><AuthCacheScope /></QueryClientProvider>);
    qc.setQueryData(["jobs"], [{ job_id: "b-job" }]);
    currentUser = { id: "user-c" };
    view.rerender(<QueryClientProvider client={qc}><AuthCacheScope /></QueryClientProvider>);
    expect(qc.getQueryData(["jobs"])).toBeUndefined();
  });
});
