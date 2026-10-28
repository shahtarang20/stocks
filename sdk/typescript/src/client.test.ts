import { describe, expect, it } from "vitest";

import { ComputeCoinClient } from "./client.js";
import {
  ConflictError,
  EndpointNotAvailableError,
  PaymentRequiredError,
  ValidationError,
} from "./errors.js";

function mockFetch(
  handler: (url: string, init: RequestInit) => { status: number; body?: unknown },
): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const { status, body } = handler(url, init ?? {});
    const text = body === undefined ? "" : JSON.stringify(body);
    return new Response(text, {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

describe("ComputeCoinClient", () => {
  it("shapes the login request and stores the token", async () => {
    let captured: { url?: string; body?: unknown } = {};
    const fetchImpl = mockFetch((url, init) => {
      captured = { url, body: JSON.parse(init.body as string) };
      return { status: 200, body: { access_token: "acc", refresh_token: "ref" } };
    });
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });
    const pair = await client.login("a@b.com", "pw");

    expect(captured.url).toBe("https://api.test/v1/auth/login");
    expect(captured.body).toEqual({ email: "a@b.com", password: "pw" });
    expect(pair.access_token).toBe("acc");
  });

  it("throws EndpointNotAvailableError on 404 for chat completions", async () => {
    const fetchImpl = mockFetch(() => ({ status: 404, body: { detail: "not implemented" } }));
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });

    await expect(
      client.chatCompletions({ model: "llama", messages: [{ role: "user", content: "hi" }] }),
    ).rejects.toBeInstanceOf(EndpointNotAvailableError);
  });

  it("throws EndpointNotAvailableError on 501 for embeddings", async () => {
    const fetchImpl = mockFetch(() => ({ status: 501, body: {} }));
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });

    await expect(client.embeddings({ model: "e5", input: "hi" })).rejects.toBeInstanceOf(
      EndpointNotAvailableError,
    );
  });
});

describe("ComputeCoinClient marketplace (Phase 3)", () => {
  it("shapes the search query (public, no auth) and parses items", async () => {
    let captured: { url?: string; auth?: string | null } = {};
    const fetchImpl = mockFetch((url, init) => {
      const headers = (init.headers ?? {}) as Record<string, string>;
      captured = { url, auth: headers["Authorization"] ?? null };
      return {
        status: 200,
        body: {
          items: [
            {
              machine_id: "m_1",
              gpu_model: "RTX 4090",
              gpu_count: 1,
              vram_mb: 24576,
              cpu_model: "EPYC",
              cpu_cores: 16,
              ram_mb: 65536,
              region: "us-east",
              verification_status: "detected",
              price_per_hour: "1.50",
              currency: "USDC_CREDITS",
              supported_workloads: ["text-generation"],
              available_slots: 1,
              max_concurrent_jobs: 1,
              reliability: { completed_jobs: 0, success_rate: null },
              last_heartbeat_at: "2026-01-01T00:00:00Z",
            },
          ],
          page: 1,
          page_size: 20,
          total: 1,
        },
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const page = await client.searchMachines({
      gpu_model: "RTX 4090",
      min_vram_mb: 16000,
      max_price_per_hour: "2.00",
      workload: "text-generation",
      page: 1,
      page_size: 20,
    });

    expect(captured.url).toContain("/v1/marketplace/machines?");
    expect(captured.url).toContain("gpu_model=RTX+4090");
    expect(captured.url).toContain("max_price_per_hour=2.00");
    expect(captured.url).toContain("min_vram_mb=16000");
    // Public endpoint: no Authorization header even when a token is set.
    expect(captured.auth).toBeNull();

    expect(page.total).toBe(1);
    const machine = page.items[0]!;
    expect(machine.machine_id).toBe("m_1");
    // Money stays a string.
    expect(machine.price_per_hour).toBe("1.50");
    expect(typeof machine.price_per_hour).toBe("string");
    // Honest: no fabricated rating.
    expect(machine.reliability?.success_rate).toBeNull();
  });

  it("filters marketplace machines by gpu_vendor and gpu_runtime", async () => {
    let capturedUrl = "";
    const fetchImpl = mockFetch((url) => {
      capturedUrl = url;
      return {
        status: 200,
        body: {
          items: [
            {
              machine_id: "m_amd",
              gpu_model: "Radeon RX 7900 XTX",
              gpu_count: 1,
              vram_mb: 24576,
              cpu_model: "AMD EPYC",
              cpu_cores: 16,
              ram_mb: 65536,
              region: "us-east",
              price_per_hour: "1.20",
              currency: "USDC_CREDITS",
              supported_workloads: ["text-generation"],
              available_slots: 1,
              max_concurrent_jobs: 1,
              gpu_vendor: "amd",
              gpu_runtime: "rocm",
              gpu_runtime_version: "6.2",
            },
          ],
          page: 1,
          page_size: 20,
          total: 1,
        },
      };
    });
    const client = new ComputeCoinClient("https://api.test", { fetch: fetchImpl });
    const page = await client.searchMachines({
      gpu_vendor: "amd",
      gpu_runtime: "rocm",
    });
    expect(capturedUrl).toContain("gpu_vendor=amd");
    expect(capturedUrl).toContain("gpu_runtime=rocm");
    expect(page.items[0]?.gpu_vendor).toBe("amd");
    expect(page.items[0]?.gpu_runtime).toBe("rocm");
  });

  it("creates a quote and keeps every breakdown money field a string", async () => {
    let captured: { url?: string; auth?: string | null; body?: unknown } = {};
    const fetchImpl = mockFetch((url, init) => {
      const headers = (init.headers ?? {}) as Record<string, string>;
      captured = {
        url,
        auth: headers["Authorization"] ?? null,
        body: JSON.parse(init.body as string),
      };
      return {
        status: 201,
        body: {
          quote_id: "q_1",
          machine_id: "m_1",
          customer_id: "c_1",
          workload_type: "text-generation",
          gpu_count: 1,
          estimated_minutes: 60,
          currency: "USDC_CREDITS",
          breakdown: {
            price_per_hour: "1.50",
            estimated_hours: "1.00",
            gpu_cost: "1.50",
            platform_fee_rate: "0.10",
            platform_fee: "0.15",
            total: "1.65",
          },
          max_authorized_spend: "5.00",
          status: "active",
          expires_at: "2026-01-01T00:02:00Z",
          created_at: "2026-01-01T00:00:00Z",
        },
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const quote = await client.createQuote({
      machine_id: "m_1",
      workload_type: "text-generation",
      estimated_minutes: 60,
      gpu_count: 1,
      max_authorized_spend: "5.00",
    });

    expect(captured.url).toBe("https://api.test/v1/quotes");
    expect(captured.auth).toBe("Bearer tok123");
    expect(captured.body).toEqual({
      machine_id: "m_1",
      workload_type: "text-generation",
      estimated_minutes: 60,
      gpu_count: 1,
      max_authorized_spend: "5.00",
    });
    expect(quote.breakdown.total).toBe("1.65");
    expect(quote.breakdown.platform_fee).toBe("0.15");
    expect(typeof quote.breakdown.total).toBe("string");
    expect(quote.max_authorized_spend).toBe("5.00");
  });

  it("surfaces a 409 no_capacity on reserve as a typed ConflictError with the detail", async () => {
    const fetchImpl = mockFetch(() => ({ status: 409, body: { detail: "no_capacity" } }));
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });

    await expect(client.reserveQuote("q_1")).rejects.toBeInstanceOf(ConflictError);
    try {
      await client.reserveQuote("q_1");
      throw new Error("expected reserveQuote to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ConflictError);
      const conflict = err as ConflictError;
      expect(conflict.statusCode).toBe(409);
      expect(conflict.code).toBe("no_capacity");
      expect(conflict.body).toEqual({ detail: "no_capacity" });
    }
  });

  it("returns a reservation on a successful reserve", async () => {
    const fetchImpl = mockFetch(() => ({
      status: 201,
      body: {
        reservation_id: "r_1",
        quote_id: "q_1",
        machine_id: "m_1",
        slots: 1,
        status: "active",
        expires_at: "2026-01-01T00:15:00Z",
        created_at: "2026-01-01T00:00:00Z",
      },
    }));
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const reservation = await client.reserveQuote("q_1");
    expect(reservation.reservation_id).toBe("r_1");
    expect(reservation.status).toBe("active");
    expect(reservation.slots).toBe(1);
  });
});

describe("ComputeCoinClient jobs (Phase 4)", () => {
  const jobBody = (overrides: Record<string, unknown> = {}) => ({
    id: "job_1",
    customer_id: "c_1",
    workload_type: "text-generation",
    model: "echo-sim-v1",
    input: { prompt: "Hello", max_tokens: 64 },
    status: "created",
    execution_mode: "simulated",
    gpu_count: 1,
    estimated_minutes: 5,
    max_cost: "5.00",
    max_runtime_seconds: 300,
    retry_policy: { max_retries: 1 },
    attempts_made: 0,
    final_cost: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  });

  it("shapes the createJob body, sends auth + Idempotency-Key, surfaces execution_mode", async () => {
    let captured: {
      url?: string;
      auth?: string | null;
      idempotency?: string | null;
      body?: unknown;
    } = {};
    const fetchImpl = mockFetch((url, init) => {
      const headers = (init.headers ?? {}) as Record<string, string>;
      captured = {
        url,
        auth: headers["Authorization"] ?? null,
        idempotency: headers["Idempotency-Key"] ?? null,
        body: JSON.parse(init.body as string),
      };
      return { status: 201, body: jobBody() };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const job = await client.createJob(
      {
        workload_type: "text-generation",
        model: "echo-sim-v1",
        input: { prompt: "Hello", max_tokens: 64 },
        gpu_count: 1,
        estimated_minutes: 5,
        max_cost: "5.00",
        max_runtime_seconds: 300,
        retry_policy: { max_retries: 1 },
      },
      "idem-abc",
    );

    expect(captured.url).toBe("https://api.test/v1/jobs");
    expect(captured.auth).toBe("Bearer tok123");
    expect(captured.idempotency).toBe("idem-abc");
    expect(captured.body).toEqual({
      workload_type: "text-generation",
      model: "echo-sim-v1",
      input: { prompt: "Hello", max_tokens: 64 },
      gpu_count: 1,
      estimated_minutes: 5,
      max_cost: "5.00",
      max_runtime_seconds: 300,
      retry_policy: { max_retries: 1 },
    });
    // Money stays a string in the request body.
    expect(typeof (captured.body as Record<string, unknown>)["max_cost"]).toBe("string");
    // Honesty stamp surfaced on the returned Job.
    expect(job.execution_mode).toBe("simulated");
  });

  it("submits job with gpu_runtime and surfaces required_gpu_runtime", async () => {
    let capturedBody: Record<string, unknown> = {};
    const fetchImpl = mockFetch((_url, init) => {
      capturedBody = JSON.parse(init.body as string);
      return {
        status: 201,
        body: jobBody({ required_gpu_runtime: "rocm", gpu_runtime: "rocm" }),
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const job = await client.createJob({
      workload_type: "text-generation",
      model: "echo-sim-v1",
      input: { prompt: "Hi" },
      gpu_count: 1,
      estimated_minutes: 5,
      max_cost: "5.00",
      max_runtime_seconds: 300,
      gpu_runtime: "rocm",
    });
    expect(capturedBody.required_gpu_runtime).toBe("rocm");
    expect(job.required_gpu_runtime).toBe("rocm");
    expect(job.gpu_runtime).toBe("rocm");
  });

  it("omits the Idempotency-Key header when no key is given", async () => {
    let idempotency: string | null = "unset";
    const fetchImpl = mockFetch((_url, init) => {
      const headers = (init.headers ?? {}) as Record<string, string>;
      idempotency = headers["Idempotency-Key"] ?? null;
      return { status: 201, body: jobBody() };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    await client.createJob({
      workload_type: "text-generation",
      model: "echo-sim-v1",
      input: { prompt: "Hi" },
      gpu_count: 1,
      estimated_minutes: 5,
      max_cost: "5.00",
      max_runtime_seconds: 300,
    });
    expect(idempotency).toBeNull();
  });

  it("surfaces a 422 invalid_input as a typed ValidationError carrying the code", async () => {
    const fetchImpl = mockFetch(() => ({ status: 422, body: { detail: "invalid_input" } }));
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const params = {
      workload_type: "text-generation",
      model: "echo-sim-v1",
      input: {},
      gpu_count: 1,
      estimated_minutes: 5,
      max_cost: "5.00",
      max_runtime_seconds: 300,
    };
    await expect(client.createJob(params)).rejects.toBeInstanceOf(ValidationError);
    try {
      await client.createJob(params);
      throw new Error("expected createJob to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ValidationError);
      const v = err as ValidationError;
      expect(v.statusCode).toBe(422);
      expect(v.code).toBe("invalid_input");
      expect(v.body).toEqual({ detail: "invalid_input" });
    }
  });

  it("parses the job events FSM history", async () => {
    let path: string | undefined;
    const fetchImpl = mockFetch((url) => {
      path = url;
      return {
        status: 200,
        body: [
          {
            job_id: "job_1",
            seq: 1,
            from_state: null,
            to_state: "created",
            actor: "customer",
            created_at: "2026-01-01T00:00:00Z",
          },
          {
            job_id: "job_1",
            seq: 2,
            from_state: "created",
            to_state: "validating",
            actor: "system",
            created_at: "2026-01-01T00:00:01Z",
          },
        ],
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const events = await client.getJobEvents("job_1");
    expect(path).toContain("/v1/jobs/job_1/events");
    expect(events).toHaveLength(2);
    expect(events[0]!.seq).toBe(1);
    expect(events[0]!.to_state).toBe("created");
    expect(events[0]!.from_state).toBeNull();
    expect(events[1]!.to_state).toBe("validating");
  });

  it("cancels a job and returns the ack", async () => {
    let captured: { method?: string | undefined; url?: string } = {};
    const fetchImpl = mockFetch((url, init) => {
      captured = { method: init.method, url };
      return {
        status: 200,
        body: { job_id: "job_1", status: "cancelled", cancellation_requested: true },
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const ack = await client.cancelJob("job_1");
    expect(captured.method).toBe("POST");
    expect(captured.url).toContain("/v1/jobs/job_1/cancel");
    expect(ack.job_id).toBe("job_1");
    expect(ack.cancellation_requested).toBe(true);
  });

  it("waitForJob polls until a terminal state, fully offline", async () => {
    const states = ["running", "verifying", "completed"];
    let i = 0;
    const fetchImpl = mockFetch(() => ({
      status: 200,
      body: jobBody({ status: states[Math.min(i++, states.length - 1)] }),
    }));
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    // poll=0 keeps the test instant.
    const job = await client.waitForJob("job_1", { poll: 0, timeout: 5 });
    expect(job.status).toBe("completed");
  });
});

describe("ComputeCoinClient billing (Phase 5)", () => {
  it("getBillingAccount parses money strings", async () => {
    let captured: { url?: string; method?: string | undefined; auth?: string | null } = {};
    const fetchImpl = mockFetch((url, init) => {
      const headers = (init.headers ?? {}) as Record<string, string>;
      captured = { url, method: init.method, auth: headers["Authorization"] ?? null };
      return {
        status: 200,
        body: {
          currency: "USDC_CREDITS",
          available: "998.35",
          reserved: "1.65",
          balance: "1000.00",
        },
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const account = await client.getBillingAccount();

    expect(captured.method).toBe("GET");
    expect(captured.url).toContain("/v1/billing/account");
    expect(captured.auth).toBe("Bearer tok123");
    // Every money field stays a string, never coerced to a number.
    expect(account.available).toBe("998.35");
    expect(account.reserved).toBe("1.65");
    expect(account.balance).toBe("1000.00");
    expect(typeof account.available).toBe("string");
    expect(account.currency).toBe("USDC_CREDITS");
  });

  it("purchaseCredits sends the amount body and returns the account", async () => {
    let captured: { url?: string; method?: string | undefined; body?: unknown } = {};
    const fetchImpl = mockFetch((url, init) => {
      captured = { url, method: init.method, body: JSON.parse(init.body as string) };
      return {
        status: 200,
        body: {
          currency: "USDC_CREDITS",
          available: "1048.35",
          reserved: "1.65",
          balance: "1050.00",
        },
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const account = await client.purchaseCredits({ amount: "50.00" });

    expect(captured.method).toBe("POST");
    expect(captured.url).toContain("/v1/billing/credits/purchase");
    // Amount stays a decimal string in the request body.
    expect(captured.body).toEqual({ amount: "50.00" });
    expect(typeof (captured.body as Record<string, unknown>)["amount"]).toBe("string");
    expect(account.available).toBe("1048.35");
  });

  it("listInvoices parses estimated vs actual totals (kept separate)", async () => {
    const fetchImpl = mockFetch((url) => {
      expect(url).toContain("/v1/invoices");
      return {
        status: 200,
        body: [
          {
            invoice_id: "inv_1",
            job_id: "job_1",
            line_items: [
              { label: "gpu_cost", amount: "1.50" },
              { label: "platform_fee", amount: "0.15" },
            ],
            estimated_total: "1.65",
            actual_total: "1.10",
            platform_fee: "0.10",
            currency: "USDC_CREDITS",
            status: "settled",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const invoices = await client.listInvoices();

    expect(invoices).toHaveLength(1);
    const inv = invoices[0]!;
    // Estimated vs actual are tracked separately -- never conflated.
    expect(inv.estimated_total).toBe("1.65");
    expect(inv.actual_total).toBe("1.10");
    expect(inv.estimated_total).not.toBe(inv.actual_total);
    expect(typeof inv.actual_total).toBe("string");
    expect(inv.line_items[0]!.label).toBe("gpu_cost");
    expect(inv.line_items[0]!.amount).toBe("1.50");
    expect(inv.status).toBe("settled");
  });

  it("requestPayout surfaces a 409 insufficient_earnings as a typed ConflictError", async () => {
    let captured: { body?: unknown } = {};
    const fetchImpl = mockFetch((url, init) => {
      expect(url).toContain("/v1/provider/payouts");
      captured = { body: JSON.parse(init.body as string) };
      return { status: 409, body: { detail: "insufficient_earnings" } };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });

    await expect(client.requestPayout({ amount: "500.00" })).rejects.toBeInstanceOf(
      ConflictError,
    );
    expect(captured.body).toEqual({ amount: "500.00" });
    try {
      await client.requestPayout({ amount: "500.00" });
      throw new Error("expected requestPayout to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ConflictError);
      const conflict = err as ConflictError;
      expect(conflict.statusCode).toBe(409);
      expect(conflict.code).toBe("insufficient_earnings");
      expect(conflict.body).toEqual({ detail: "insufficient_earnings" });
    }
  });

  it("requestPayout returns the payout on success", async () => {
    const fetchImpl = mockFetch(() => ({
      status: 201,
      body: {
        payout_id: "po_1",
        amount: "10.00",
        currency: "USDC_CREDITS",
        status: "requested",
        created_at: "2026-01-01T00:00:00Z",
      },
    }));
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const payout = await client.requestPayout({ amount: "10.00" });
    expect(payout.payout_id).toBe("po_1");
    expect(payout.amount).toBe("10.00");
    expect(payout.status).toBe("requested");
  });

  it("purchaseCredits surfaces a 402 insufficient_credits as a typed PaymentRequiredError", async () => {
    const fetchImpl = mockFetch(() => ({
      status: 402,
      body: { detail: "insufficient_credits" },
    }));
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });

    await expect(client.purchaseCredits({ amount: "50.00" })).rejects.toBeInstanceOf(
      PaymentRequiredError,
    );
    try {
      await client.purchaseCredits({ amount: "50.00" });
      throw new Error("expected purchaseCredits to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(PaymentRequiredError);
      const pr = err as PaymentRequiredError;
      expect(pr.statusCode).toBe(402);
      expect(pr.code).toBe("insufficient_credits");
    }
  });

  it("getProviderEarnings parses live ledger figures", async () => {
    const fetchImpl = mockFetch((url) => {
      expect(url).toContain("/v1/provider/earnings");
      return {
        status: 200,
        body: {
          currency: "USDC_CREDITS",
          available: "12.34",
          lifetime: "99.99",
          recent: [
            {
              entry_id: "e_1",
              transaction_id: "t_1",
              kind: "capture",
              direction: "credit",
              amount: "1.35",
              currency: "USDC_CREDITS",
              reference: "job:job_1",
              created_at: "2026-01-01T00:00:00Z",
            },
          ],
        },
      };
    });
    const client = new ComputeCoinClient("https://api.test", {
      fetch: fetchImpl,
      accessToken: "tok123",
    });
    const earnings = await client.getProviderEarnings();
    expect(earnings.available).toBe("12.34");
    expect(earnings.lifetime).toBe("99.99");
    expect(typeof earnings.available).toBe("string");
    expect(earnings.recent[0]!.amount).toBe("1.35");
    expect(earnings.recent[0]!.direction).toBe("credit");
  });
});
