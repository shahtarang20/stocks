// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearStoredTokens, request, SESSION_EXPIRED_EVENT, setStoredTokens } from "./api";

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
const bearer = (init?: RequestInit) => (init?.headers as Record<string, string> | undefined)?.Authorization;

describe("silent session refresh on 401 (access token expired while the page stayed open)", () => {
  beforeEach(() => clearStoredTokens());
  afterEach(() => vi.unstubAllGlobals());

  function server(opts: { refreshOk?: boolean } = {}) {
    const { refreshOk = true } = opts;
    const calls: Array<{ url: string; auth?: string }> = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: url.replace(/^.*?(?=\/)/, "").replace(/^\/\/[^/]+/, ""), auth: bearer(init) });
      if (url.endsWith("/auth/refresh")) {
        return refreshOk
          ? json(200, { access_token: "access-new", refresh_token: "refresh-2", csrf_token: "csrf-2", token_type: "bearer", expires_in: 900 })
          : json(401, { detail: "Invalid refresh token" });
      }
      return bearer(init) === "Bearer access-new" ? json(200, { ok: true }) : json(401, { detail: "Could not validate credentials" });
    });
    vi.stubGlobal("fetch", fetchMock);
    return { calls, fetchMock };
  }

  it("refreshes once and replays the request with the new token", async () => {
    setStoredTokens("access-old", "refresh-1", "csrf-1");
    const { calls } = server();
    await expect(request("/v1/jobs", { token: "access-old" })).resolves.toEqual({ ok: true });
    expect(calls.map((c) => c.url.split("/").slice(-2).join("/"))).toEqual(["v1/jobs", "auth/refresh", "v1/jobs"]);
    expect(calls[2].auth).toBe("Bearer access-new");
  });

  it("shares one refresh between concurrent failures", async () => {
    setStoredTokens("access-old", "refresh-1", "csrf-1");
    const { calls } = server();
    await Promise.all([request("/v1/jobs"), request("/v1/usage"), request("/projects")]);
    expect(calls.filter((c) => c.url.endsWith("/auth/refresh"))).toHaveLength(1);
  });

  it("swaps a stale token captured by a component for the live one, without another refresh", async () => {
    setStoredTokens("access-old", "refresh-1", "csrf-1");
    const { calls } = server();
    await request("/v1/jobs", { token: "access-old" });
    calls.length = 0;
    await request("/v1/usage", { token: "access-old" }); // component still holds the old JWT
    expect(calls).toHaveLength(1);
    expect(calls[0].auth).toBe("Bearer access-new");
  });

  it("signals session expiry and surfaces the 401 when the refresh token is also dead", async () => {
    setStoredTokens("access-old", "refresh-1", "csrf-1");
    server({ refreshOk: false });
    const expired = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    await expect(request("/v1/jobs")).rejects.toMatchObject({ status: 401 });
    window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it("never tries to refresh for /auth/* calls (a wrong password must not loop)", async () => {
    const { calls } = server();
    await expect(request("/auth/login", { method: "POST", body: {} })).rejects.toMatchObject({ status: 401 });
    expect(calls).toHaveLength(1);
  });
});
