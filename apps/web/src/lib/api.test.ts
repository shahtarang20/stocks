import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, clearStoredTokens, request, setStoredTokens } from "./api";

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

async function failure(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected the request to fail");
}

describe("request error messages", () => {
  beforeEach(() => clearStoredTokens());
  afterEach(() => vi.unstubAllGlobals());

  it("turns a FastAPI 422 detail list into a readable message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse(422, { detail: [{ loc: ["body", "email"], msg: "value is not a valid email address", type: "x" }] }),
    ));
    const err = await failure(request("/auth/register", { method: "POST", body: {} }));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.message).toBe("email: value is not a valid email address");
  });

  it("keeps a string detail as the message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(402, { detail: "insufficient_credits" })));
    const err = await failure(request("/v1/jobs", { method: "POST", body: {} }));
    expect(err.message).toBe("insufficient_credits");
  });
});

describe("logout", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the refresh token (no CSRF cookie needed) and only then clears local credentials", async () => {
    setStoredTokens("access-1", "refresh-1", "csrf-1");
    const fetchMock = vi.fn(async () => jsonResponse(200, { message: "Logged out" }));
    vi.stubGlobal("fetch", fetchMock);

    await api.logout();

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ refresh_token: "refresh-1" });
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer access-1");

    // credentials are gone afterwards, even though the request had to read them first
    fetchMock.mockClear();
    await request("/auth/me");
    const [, init2] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init2.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("clears local credentials even when the server call fails", async () => {
    setStoredTokens("access-1", "refresh-1", "csrf-1");
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(500, { detail: "boom" })));
    await api.logout().catch(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, {})));
    await request("/auth/me");
    const f = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    const [, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});

describe("CSRF token recovery after a reload (web and API on different origins)", () => {
  beforeEach(() => clearStoredTokens());
  afterEach(() => vi.unstubAllGlobals());

  it("fetches the token from /auth/csrf before a cookie-authenticated mutation", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return url.endsWith("/auth/csrf") ? jsonResponse(200, { csrf_token: "csrf-from-api" }) : jsonResponse(200, {});
    }));
    await request("/projects", { method: "POST", body: { name: "p" } });
    expect(calls[0][0]).toMatch(/\/auth\/csrf$/);
    expect(calls[0][1]?.credentials).toBe("include");
    expect((calls[1][1]?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-from-api");
    // remembered: a second mutation does not ask again
    await request("/projects", { method: "POST", body: { name: "q" } });
    expect(calls.filter(([u]) => u.endsWith("/auth/csrf"))).toHaveLength(1);
  });

  it("does not look it up for login, for bearer calls, or when a token is already held", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await request("/auth/login", { method: "POST", body: {} });
    setStoredTokens("access-1", "refresh-1", "csrf-1");
    await request("/projects", { method: "POST", body: {} });
    clearStoredTokens();
    setStoredTokens(null, null, "csrf-only");
    await request("/projects", { method: "POST", body: {} });
    expect(fetchMock.mock.calls.some((c) => String((c as unknown[])[0]).endsWith("/auth/csrf"))).toBe(false);
  });

  it("still sends the mutation (and lets the API answer) when there is no session", async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.endsWith("/auth/csrf") ? jsonResponse(401, { detail: "Not authenticated" }) : jsonResponse(401, { detail: "Not authenticated" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await failure(request("/projects", { method: "POST", body: {} }));
    expect(err.status).toBe(401);
  });

  it("answers a cookie refresh locally (401) when there is no session to refresh", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(401, { detail: "Not authenticated" }));
    vi.stubGlobal("fetch", fetchMock);
    const err = await failure(request("/auth/refresh", { method: "POST" }));
    expect(err.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toMatch(/\/auth\/csrf$/);
  });
});

describe("settlement requests", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the payload as one JSON object, not a JSON-encoded string", async () => {
    setStoredTokens("access-1", "refresh-1", "csrf-1");
    const fetchMock = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    const payload = { job_id: "j1", final_cost_units: "10", platform_fee_units: "1" };

    await api.issueSettlementVoucher("access-1", payload);
    await api.confirmSettlement("access-1", { job_id: "j1", settle_tx_hash: "0x" + "a".repeat(64) });
    await api.confirmRefund("access-1", { job_id: "j1", refund_tx_hash: "0x" + "b".repeat(64) });

    for (const call of fetchMock.mock.calls as unknown as [string, RequestInit][]) {
      expect(typeof JSON.parse(call[1].body as string)).toBe("object");
    }
    expect(JSON.parse((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body as string)).toEqual(payload);
  });
});
