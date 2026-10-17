/**
 * ComputeCoin TypeScript client (fetch-based), mirroring the Python SDK.
 *
 * The OpenAI-compatible inference endpoints may not be live yet; on 404/501 the
 * client throws EndpointNotAvailableError instead of fabricating a result.
 */

import {
  ApiError,
  AuthenticationError,
  ConflictError,
  EndpointNotAvailableError,
  PaymentRequiredError,
  RateLimitError,
  ValidationError,
} from "./errors.js";
import type {
  ApiKey,
  ArtifactDownload,
  BillingAccount,
  CancelAck,
  ChatCompletion,
  ChatMessage,
  ClientOptions,
  CreateJobParams,
  CreateQuoteParams,
  Dispute,
  EmbeddingResponse,
  Invoice,
  Job,
  JobArtifact,
  JobEvent,
  JobLogs,
  LedgerEntry,
  MachinePage,
  MarketplaceMachine,
  OpenDisputeParams,
  Payout,
  Project,
  ProviderEarnings,
  PurchaseCreditsParams,
  Quote,
  ReleaseResult,
  RequestPayoutParams,
  Reservation,
  SearchMachinesParams,
  TokenPair,
  Usage,
  WaitForJobOptions,
} from "./types.js";

/** FSM states that are terminal -- a job never leaves these (Phase 4 contract). */
const TERMINAL_JOB_STATES = new Set([
  "completed",
  "failed",
  "cancelled",
  "timed_out",
  "disputed",
]);

export const DEFAULT_BASE_URL = "https://api.computecoin.example";

export class ComputeCoinClient {
  private readonly baseUrl: string;
  private accessToken: string | undefined;
  private readonly apiKey: string | undefined;
  private readonly fetchImpl: typeof fetch;

  constructor(baseUrl: string = DEFAULT_BASE_URL, options: ClientOptions = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.accessToken = options.accessToken;
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    if (typeof this.fetchImpl !== "function") {
      throw new Error("No fetch implementation available; pass options.fetch.");
    }
  }

  setAccessToken(token: string): void {
    this.accessToken = token;
  }

  private headers(auth: boolean): Record<string, string> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (auth) {
      const bearer = this.apiKey ?? this.accessToken;
      if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
    }
    return headers;
  }

  private async request<T>(
    method: string,
    path: string,
    opts: {
      body?: unknown;
      auth?: boolean;
      query?: Record<string, unknown>;
      headers?: Record<string, string>;
    } = {},
  ): Promise<T> {
    const auth = opts.auth ?? true;
    const headers = this.headers(auth);
    if (opts.headers) Object.assign(headers, opts.headers);
    const init: RequestInit = {
      method,
      headers,
    };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);

    let url = `${this.baseUrl}${path}`;
    if (opts.query) {
      const search = new URLSearchParams();
      for (const [key, value] of Object.entries(opts.query)) {
        if (value !== undefined && value !== null) search.append(key, String(value));
      }
      const qs = search.toString();
      if (qs) url += `?${qs}`;
    }

    const resp = await this.fetchImpl(url, init);
    return this.handle<T>(resp);
  }

  private async handle<T>(resp: Response): Promise<T> {
    if (resp.ok) {
      const text = await resp.text();
      return (text ? JSON.parse(text) : undefined) as T;
    }

    let body: unknown;
    let message: string;
    try {
      body = await resp.json();
      const b = body as Record<string, unknown>;
      message = (b["detail"] as string) ?? (b["message"] as string) ?? JSON.stringify(b);
    } catch {
      body = undefined;
      message = resp.statusText;
    }

    const status = resp.status;
    if (status === 404 || status === 501) {
      throw new EndpointNotAvailableError(
        status,
        `endpoint not available yet (no capacity / not implemented): ${message}`,
        body,
      );
    }
    if (status === 401 || status === 403) throw new AuthenticationError(status, message, body);
    if (status === 402) {
      let code: string | undefined;
      if (body && typeof body === "object") {
        const b = body as Record<string, unknown>;
        code = (b["detail"] as string) ?? (b["code"] as string) ?? undefined;
      }
      throw new PaymentRequiredError(status, message, body, code);
    }
    if (status === 429) throw new RateLimitError(status, message, body);
    if (status === 409) {
      let code: string | undefined;
      if (body && typeof body === "object") {
        const b = body as Record<string, unknown>;
        code = (b["detail"] as string) ?? (b["code"] as string) ?? undefined;
      }
      throw new ConflictError(status, message, body, code);
    }
    if (status === 422) {
      let code: string | undefined;
      if (body && typeof body === "object") {
        const b = body as Record<string, unknown>;
        code = (b["code"] as string) ?? (b["detail"] as string) ?? undefined;
      }
      throw new ValidationError(status, message, body, code);
    }
    throw new ApiError(status, message, body);
  }

  // -- auth ---------------------------------------------------------------- //
  async register(email: string, password: string): Promise<unknown> {
    return this.request("POST", "/v1/auth/register", {
      body: { email, password },
      auth: false,
    });
  }

  async login(email: string, password: string): Promise<TokenPair> {
    const pair = await this.request<TokenPair>("POST", "/v1/auth/login", {
      body: { email, password },
      auth: false,
    });
    this.accessToken = pair.access_token;
    return pair;
  }

  async refresh(refreshToken: string): Promise<TokenPair> {
    const pair = await this.request<TokenPair>("POST", "/v1/auth/refresh", {
      body: { refresh_token: refreshToken },
      auth: false,
    });
    this.accessToken = pair.access_token;
    return pair;
  }

  // -- projects ------------------------------------------------------------ //
  async listProjects(): Promise<Project[]> {
    return this.request<Project[]>("GET", "/v1/projects");
  }

  async createProject(name: string): Promise<Project> {
    return this.request<Project>("POST", "/v1/projects", { body: { name } });
  }

  async createApiKey(projectId: string, name?: string): Promise<ApiKey> {
    return this.request<ApiKey>("POST", `/v1/projects/${projectId}/api-keys`, {
      body: { name: name ?? null },
    });
  }

  // -- OpenAI-compatible inference ----------------------------------------- //
  async chatCompletions(params: {
    model: string;
    messages: ChatMessage[];
    [key: string]: unknown;
  }): Promise<ChatCompletion> {
    return this.request<ChatCompletion>("POST", "/v1/chat/completions", { body: params });
  }

  async embeddings(params: {
    model: string;
    input: unknown;
    [key: string]: unknown;
  }): Promise<EmbeddingResponse> {
    return this.request<EmbeddingResponse>("POST", "/v1/embeddings", { body: params });
  }

  // -- Phase 3: marketplace (PUBLIC) --------------------------------------- //
  /**
   * Browse online, priced, available machines. No auth required.
   * `max_price_per_hour` is a decimal string (money).
   */
  async searchMachines(params: SearchMachinesParams = {}): Promise<MachinePage> {
    return this.request<MachinePage>("GET", "/v1/marketplace/machines", {
      auth: false,
      query: params as Record<string, unknown>,
    });
  }

  async getMarketplaceMachine(machineId: string): Promise<MarketplaceMachine> {
    return this.request<MarketplaceMachine>(
      "GET",
      `/v1/marketplace/machines/${machineId}`,
      { auth: false },
    );
  }

  // -- Phase 3: quotes (customer auth) ------------------------------------- //
  /**
   * Create an itemized, time-limited, price-snapshotted quote. Every money
   * line in the returned `breakdown` is a decimal string -- no number coercion.
   */
  async createQuote(params: CreateQuoteParams): Promise<Quote> {
    return this.request<Quote>("POST", "/v1/quotes", { body: params });
  }

  async getQuote(quoteId: string): Promise<Quote> {
    return this.request<Quote>("GET", `/v1/quotes/${quoteId}`);
  }

  async listQuotes(): Promise<Quote[]> {
    return this.request<Quote[]>("GET", "/v1/quotes");
  }

  // -- Phase 3: reservations (customer auth) ------------------------------- //
  /**
   * Atomically convert a quote into a capacity reservation. On a 409 the
   * server reports a specific reason (`no_capacity`, `quote_expired`,
   * `quote_already_reserved`, `budget_exceeded`); the client throws
   * {@link ConflictError} carrying that `code` -- never a fabricated success.
   */
  async reserveQuote(quoteId: string): Promise<Reservation> {
    return this.request<Reservation>("POST", `/v1/quotes/${quoteId}/reserve`, {
      body: {},
    });
  }

  async listReservations(): Promise<Reservation[]> {
    return this.request<Reservation[]>("GET", "/v1/reservations");
  }

  /**
   * Release a held reservation. Idempotent-ish: releasing an already released
   * reservation returns a 200/204 rather than erroring.
   */
  async releaseReservation(reservationId: string): Promise<ReleaseResult> {
    return this.request<ReleaseResult>(
      "POST",
      `/v1/reservations/${reservationId}/release`,
      { body: {} },
    );
  }

  // -- Phase 4: jobs (customer auth) --------------------------------------- //
  /**
   * Submit a job (POST /v1/jobs). `max_cost` is a decimal string (money), sent
   * verbatim -- never coerced to a number. When `idempotencyKey` is given it is
   * sent as the `Idempotency-Key` header, so the same (customer, key) returns
   * the SAME job rather than creating a duplicate.
   *
   * On a 422 the server reports `invalid_input` / `model_not_supported` and on
   * a 409 `budget_exceeded`; these surface as a typed {@link ValidationError} /
   * {@link ConflictError} carrying `.code` -- the call never fabricates success.
   */
  async createJob(params: CreateJobParams, idempotencyKey?: string): Promise<Job> {
    const opts: { body: unknown; headers?: Record<string, string> } = { body: params };
    if (idempotencyKey !== undefined) {
      opts.headers = { "Idempotency-Key": idempotencyKey };
    }
    return this.request<Job>("POST", "/v1/jobs", opts);
  }

  async getJob(id: string): Promise<Job> {
    return this.request<Job>("GET", `/v1/jobs/${id}`);
  }

  async listJobs(): Promise<Job[]> {
    return this.request<Job[]>("GET", "/v1/jobs");
  }

  /** Ordered FSM history (append-only job_events) for a job. */
  async getJobEvents(id: string): Promise<JobEvent[]> {
    return this.request<JobEvent[]>("GET", `/v1/jobs/${id}/events`);
  }

  async getJobLogs(id: string): Promise<JobLogs> {
    const data = await this.request<JobLogs | string[]>("GET", `/v1/jobs/${id}/logs`);
    // The backend may return either a bare list of lines or {"lines": [...]}.
    return Array.isArray(data) ? { lines: data } : data;
  }

  async getJobArtifacts(id: string): Promise<JobArtifact[]> {
    return this.request<JobArtifact[]>("GET", `/v1/jobs/${id}/artifacts`);
  }

  /** Fetch a short-lived signed download URL for an artifact (owner only). */
  async getArtifactDownloadUrl(
    id: string,
    artifactId: string,
  ): Promise<ArtifactDownload> {
    return this.request<ArtifactDownload>(
      "GET",
      `/v1/jobs/${id}/artifacts/${artifactId}/download`,
    );
  }

  /** Request cancellation; honored at the next safe point (-> CANCELLED). */
  async cancelJob(id: string): Promise<CancelAck> {
    const data = await this.request<CancelAck | undefined>(
      "POST",
      `/v1/jobs/${id}/cancel`,
      { body: {} },
    );
    return data ?? { job_id: id, cancellation_requested: true };
  }

  /**
   * Poll {@link getJob} until the job reaches a terminal FSM state
   * (`completed`/`failed`/`cancelled`/`timed_out`/`disputed`, lowercase per the
   * Phase 4 contract). Rejects if still non-terminal after `timeout` seconds.
   * Uses the injected fetch, so tests stay fully offline.
   */
  async waitForJob(id: string, options: WaitForJobOptions = {}): Promise<Job> {
    const poll = options.poll ?? 2;
    const timeout = options.timeout ?? 300;
    const deadline = Date.now() + timeout * 1000;
    for (;;) {
      const job = await this.getJob(id);
      if (TERMINAL_JOB_STATES.has((job.status ?? "").toLowerCase())) return job;
      if (Date.now() >= deadline) {
        throw new Error(
          `job ${id} did not reach a terminal state within ${timeout}s ` +
            `(last status: ${job.status})`,
        );
      }
      await new Promise((resolve) => setTimeout(resolve, poll * 1000));
    }
  }

  // -- Phase 5: billing (customer auth) ------------------------------------ //
  // The MONEY layer. Every amount in and out is a decimal string, sent/kept
  // verbatim -- never coerced to a number. Currency "USDC_CREDITS" is a
  // SIMULATED test credit (no real payment rail). A 402 insufficient_credits
  // throws {@link PaymentRequiredError} carrying `.code` and a 409 (e.g.
  // insufficient_earnings) throws {@link ConflictError} carrying `.code` -- the
  // call never fabricates a success (no overspend, no fake payout).

  /** Current billing account snapshot (spendable, reserved, total). */
  async getBillingAccount(): Promise<BillingAccount> {
    return this.request<BillingAccount>("GET", "/v1/billing/account");
  }

  /**
   * Top up simulated credits. `amount` is a decimal string, sent verbatim.
   * Returns the updated account.
   */
  async purchaseCredits(params: PurchaseCreditsParams): Promise<BillingAccount> {
    return this.request<BillingAccount>("POST", "/v1/billing/credits/purchase", {
      body: params,
    });
  }

  /** The customer's own double-entry ledger entries (append-only). */
  async getLedger(): Promise<LedgerEntry[]> {
    return this.request<LedgerEntry[]>("GET", "/v1/billing/ledger");
  }

  /** Spend summary + per-job usage records (estimated vs actual kept separate). */
  async getUsage(): Promise<Usage> {
    return this.request<Usage>("GET", "/v1/usage");
  }

  /** Itemized invoices, one per settled job. */
  async listInvoices(): Promise<Invoice[]> {
    return this.request<Invoice[]>("GET", "/v1/invoices");
  }

  async getInvoice(id: string): Promise<Invoice> {
    return this.request<Invoice>("GET", `/v1/invoices/${id}`);
  }

  /** Open a dispute (status `open`) on a settled job. */
  async openDispute(jobId: string, params: OpenDisputeParams): Promise<Dispute> {
    return this.request<Dispute>("POST", `/v1/jobs/${jobId}/dispute`, { body: params });
  }

  async listDisputes(): Promise<Dispute[]> {
    return this.request<Dispute[]>("GET", "/v1/disputes");
  }

  async getDispute(id: string): Promise<Dispute> {
    return this.request<Dispute>("GET", `/v1/disputes/${id}`);
  }

  // -- Phase 5: provider billing (provider auth) --------------------------- //
  /** LIVE accrued earnings from the ledger (available + lifetime). */
  async getProviderEarnings(): Promise<ProviderEarnings> {
    return this.request<ProviderEarnings>("GET", "/v1/provider/earnings");
  }

  /**
   * Request a payout against accrued earnings. `amount` is a decimal string,
   * sent verbatim. On a 409 the server reports `insufficient_earnings`
   * (amount > available); the client throws {@link ConflictError} carrying that
   * `code` -- it never fabricates a settled payout.
   */
  async requestPayout(params: RequestPayoutParams): Promise<Payout> {
    return this.request<Payout>("POST", "/v1/provider/payouts", { body: params });
  }

  async listPayouts(): Promise<Payout[]> {
    return this.request<Payout[]>("GET", "/v1/provider/payouts");
  }
}
