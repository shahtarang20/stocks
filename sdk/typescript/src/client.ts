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
  ChatCompletionCreateParams,
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
  PrivacyVerifierKey,
  PrivateFanoutSubmission,
  Project,
  ProviderEarnings,
  PurchaseCreditsParams,
  Quote,
  ReleaseResult,
  RequestPayoutParams,
  Reservation,
  SearchMachinesParams,
  SubmitPrivateFanoutParams,
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

/** A readable message from an error body: `detail` is a string for most errors, but an object
 * (`{"error": {"message", "code"}}` for inference) or an array (request validation) for others. */
function errorMessage(b: Record<string, unknown>): string {
  const detail = b["detail"];
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    const err = d["error"] && typeof d["error"] === "object" ? (d["error"] as Record<string, unknown>) : d;
    return typeof err["message"] === "string" && err["message"] ? err["message"] : JSON.stringify(detail);
  }
  if (detail) return JSON.stringify(detail);
  return typeof b["message"] === "string" && b["message"] ? b["message"] : JSON.stringify(b);
}

/** The machine-readable reason (`insufficient_credits`, `budget_exceeded`...), always a string or undefined. */
function errorCode(body: unknown, preferBodyCode = false): string | undefined {
  if (!body || typeof body !== "object") return undefined;
  const b = body as Record<string, unknown>;
  const detail = b["detail"];
  let fromDetail: unknown;
  if (typeof detail === "string") fromDetail = detail;
  else if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    const err = d["error"] && typeof d["error"] === "object" ? (d["error"] as Record<string, unknown>) : d;
    fromDetail = err["code"];
  }
  const order = preferBodyCode ? [b["code"], fromDetail] : [fromDetail, b["code"]];
  for (const c of order) if (typeof c === "string" && c) return c;
  return undefined;
}

export class ComputeCoinClient {
  private readonly baseUrl: string;
  private accessToken: string | undefined;
  private readonly apiKey: string | undefined;
  private readonly fetchImpl: typeof fetch;
  private readonly guardianPin: string | undefined;
  private readonly requireGuardianPin: boolean;
  private readonly timeoutMs: number;

  constructor(baseUrl: string = DEFAULT_BASE_URL, options: ClientOptions = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.accessToken = options.accessToken;
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    this.guardianPin = options.guardianPin;
    this.requireGuardianPin = options.requireGuardianPin ?? false;
    this.timeoutMs = options.timeoutMs ?? 30_000;
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
    // Without a timeout a stalled connection hangs the caller forever (the Python SDK defaults to 30 s).
    if (this.timeoutMs > 0 && typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
      init.signal = AbortSignal.timeout(this.timeoutMs);
    }

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
      message = errorMessage(b);
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
      throw new PaymentRequiredError(status, message, body, errorCode(body));
    }
    if (status === 429) throw new RateLimitError(status, message, body);
    if (status === 409) {
      throw new ConflictError(status, message, body, errorCode(body));
    }
    if (status === 422) {
      throw new ValidationError(status, message, body, errorCode(body, true));
    }
    throw new ApiError(status, message, body);
  }

  // -- auth ---------------------------------------------------------------- //
  async register(email: string, password: string): Promise<unknown> {
    return this.request("POST", "/auth/register", {
      body: { email, password },
      auth: false,
    });
  }

  async login(email: string, password: string): Promise<TokenPair> {
    const pair = await this.request<TokenPair>("POST", "/auth/login", {
      body: { email, password },
      auth: false,
    });
    this.accessToken = pair.access_token;
    return pair;
  }

  async refresh(refreshToken: string): Promise<TokenPair> {
    const pair = await this.request<TokenPair>("POST", "/auth/refresh", {
      body: { refresh_token: refreshToken },
      auth: false,
    });
    this.accessToken = pair.access_token;
    return pair;
  }

  // -- projects ------------------------------------------------------------ //
  async listProjects(): Promise<Project[]> {
    return this.request<Project[]>("GET", "/projects");
  }

  async createProject(name: string): Promise<Project> {
    return this.request<Project>("POST", "/projects", { body: { name } });
  }

  async createApiKey(projectId: string, name?: string): Promise<ApiKey> {
    const k = await this.request<ApiKey>("POST", `/projects/${projectId}/api-keys`, {
      body: { name: name ?? null },
    });
    // The API names the one-time secret `api_key`; expose it as `key` as documented.
    return k.key === undefined && k.api_key !== undefined ? { ...k, key: k.api_key } : k;
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
    const body: Record<string, unknown> = { ...params };
    if (params.gpu_runtime && !params.required_gpu_runtime) {
      body.required_gpu_runtime = params.gpu_runtime;
    }
    const opts: { body: unknown; headers?: Record<string, string> } = { body };
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
    return data ?? { job_id: id, cancel_requested: true, cancellation_requested: true };
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

  /**
   * The customer's own double-entry ledger entries (append-only), newest first. The API paginates
   * (50 per page by default); this returns the entries of the requested page.
   */
  async getLedger(params: { page?: number; page_size?: number } = {}): Promise<LedgerEntry[]> {
    const data = await this.request<LedgerEntry[] | { items?: LedgerEntry[] }>("GET", "/v1/billing/ledger", {
      query: params as Record<string, unknown>,
    });
    return Array.isArray(data) ? data : data.items ?? [];
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
  // -- U18: private chat streaming and private Kit fan-out (Node only: uses node:crypto) -------- //
  async getPrivacyVerifierKey(): Promise<PrivacyVerifierKey> {
    return this.request<PrivacyVerifierKey>("GET", "/v1/privacy/verifier-key");
  }

  private async guardianSet(
    guardians?: Record<string, string>,
    threshold?: number,
  ): Promise<{ guardians: Record<string, string>; threshold: number }> {
    const { checkGuardianPin } = await import("./private.js");
    if (!guardians || threshold === undefined) {
      const keys = await this.getPrivacyVerifierKey();
      guardians = guardians ?? keys.guardians ?? {};
      threshold = threshold ?? keys.threshold ?? 2;
      checkGuardianPin(guardians, threshold, this.guardianPin, this.requireGuardianPin);
    } else if (this.guardianPin) {
      checkGuardianPin(guardians, threshold, this.guardianPin);
    }
    return { guardians, threshold };
  }

  /**
   * `client.chat.completions.create({model, messages, stream: true, isolation_tier: "sealed"})` returns an async iterable of
   * DECRYPTED text deltas (messages encrypted here, chunks opened and un-padded here). `stream: false` returns the
   * ChatCompletion as `chatCompletions` does. A non-private `stream: true` yields plain text deltas. With the platform's
   * PRIVACY_STREAMING_ENABLED off the server answers 400 `streaming_not_supported_for_private`, thrown as an ApiError.
   */
  readonly chat = {
    completions: {
      create: (params: ChatCompletionCreateParams): any => this.chatCreate(params),
    },
  };

  private chatCreate(params: ChatCompletionCreateParams): any {
    const { stream, padBytes, guardians, threshold, ...rest } = params;
    const tier = String(rest.isolation_tier ?? "").toLowerCase();
    const isPrivate = ["sealed", "sealed_pro", "confidential"].includes(tier);
    if (!stream) {
      if (isPrivate) throw new Error("private chat is streamed: pass stream: true (or submit a private job for a one-shot result)");
      return this.chatCompletions(rest as { model: string; messages: ChatMessage[] });
    }
    return isPrivate ? this.privateChatStream(rest, padBytes, guardians, threshold) : this.plainChatStream(rest);
  }

  private async streamBody(payload: Record<string, unknown>): Promise<ReadableStream<Uint8Array>> {
    const resp = await this.fetchImpl(`${this.baseUrl}/v1/chat/completions`, {
      method: "POST",
      headers: this.headers(true),
      body: JSON.stringify(payload),
    });
    if (!resp.ok) await this.handle(resp); // throws the typed error
    if (!resp.body) throw new Error("streaming is not supported by this fetch implementation");
    return resp.body;
  }

  private async *plainChatStream(rest: Record<string, unknown>): AsyncGenerator<string> {
    const { iterSseData } = await import("./private.js");
    const body = await this.streamBody({ ...rest, stream: true });
    for await (const ev of iterSseData(body)) {
      const e = ev as { error?: { message?: string; code?: string }; choices?: Array<{ delta?: { content?: string } }> };
      if (e.error) throw new ApiError(200, String(e.error.message ?? e.error.code), e);
      for (const c of e.choices ?? []) if (c.delta?.content) yield c.delta.content;
    }
  }

  private async *privateChatStream(
    rest: Record<string, unknown>,
    padBytes: number | undefined,
    guardians: Record<string, string> | undefined,
    threshold: number | undefined,
  ): AsyncGenerator<string> {
    const { generateX25519Keypair, preparePrivateInput, iterPrivateText, buildPrivateChatInput, DEFAULT_PAD_BYTES } = await import("./private.js");
    const g = await this.guardianSet(guardians, threshold);
    const plain = buildPrivateChatInput(rest.messages as ChatMessage[], {
      max_tokens: rest.max_tokens as number | undefined,
      temperature: rest.temperature as number | undefined,
    });
    const kp = generateX25519Keypair();
    const prepared = preparePrivateInput(plain, g.guardians, g.threshold, true);
    const body = await this.streamBody({
      ...rest,
      messages: [{ role: "user", content: "sealed" }], // placeholder; the real messages are in encrypted_input
      stream: true,
      encrypted_input: prepared.encrypted_input,
      dk_shares: prepared.dk_shares,
      result_public_key: kp.publicKeyHex,
    });
    yield* iterPrivateText(body, kp.privateKeyHex, padBytes ?? DEFAULT_PAD_BYTES);
  }

  /**
   * Encrypt one plaintext per part here and POST `/v1/jobs/v2/fanout` with `private_parts`. Keep `resultPrivateKeys`
   * (one per part, same order): they are the only way to open the parts' sealed outputs.
   */
  async submitPrivateFanout(params: SubmitPrivateFanoutParams): Promise<PrivateFanoutSubmission> {
    const { preparePrivateFanoutParts } = await import("./private.js");
    const tier = params.isolationTier ?? "sealed";
    if (!["sealed", "sealed_pro", "confidential"].includes(tier)) throw new Error("a private fan-out needs a private isolationTier");
    const g = await this.guardianSet(params.guardians, params.threshold);
    const { parts, resultPrivateKeys } = preparePrivateFanoutParts(params.parts, g.guardians, g.threshold);
    const body: Record<string, unknown> = {
      spec: params.spec,
      isolation_tier: tier,
      required_gpu_runtime: params.requiredGpuRuntime ?? "cuda",
      private_parts: parts,
    };
    if (params.projectId !== undefined) body["project_id"] = params.projectId;
    if (params.machineId !== undefined) body["machine_id"] = params.machineId;
    const fanout = await this.request<Record<string, unknown>>("POST", "/v1/jobs/v2/fanout", {
      body,
      ...(params.idempotencyKey !== undefined ? { headers: { "Idempotency-Key": params.idempotencyKey } } : {}),
    });
    return { fanout, resultPrivateKeys };
  }
}
