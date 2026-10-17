/**
 * Typed fetch client for the ComputeCoin FastAPI backend.
 *
 * Contract (backend depends on these shapes — do not change casually):
 *   POST {API}/auth/register  {email, password}            -> 201 / user
 *   POST {API}/auth/login     {email, password}            -> {access_token, refresh_token}
 *   POST {API}/auth/refresh   {refresh_token}              -> {access_token, refresh_token}
 *   GET  {API}/projects       (Authorization: Bearer ...)  -> Project[]
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "http://localhost:8000";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
}

export interface AuthCredentials {
  email: string;
  password: string;
}

export interface RegisteredUser {
  id?: string;
  email: string;
  // Backend may return more fields; kept permissive on purpose for Phase 1.
  [key: string]: unknown;
}

export interface Project {
  id: string;
  name: string;
  status?: string;
  created_at?: string;
  description?: string | null;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Phase 2 — Provider onboarding & machine registration types.
// Shapes mirror docs/api/phase2-provider-protocol.md exactly.
// ---------------------------------------------------------------------------

export interface ProviderRegisterInput {
  display_name?: string;
  contact_email?: string;
}

export interface Provider {
  provider_id: string;
  display_name: string | null;
  status: string;
  created_at: string;
  [key: string]: unknown;
}

/** Response from POST /provider/enrollment-tokens — includes the raw token ONCE. */
export interface EnrollmentTokenCreated {
  enrollment_token_id: string;
  /** Raw `enr_...` secret — shown exactly once, never returned again. */
  enrollment_token: string;
  expires_at: string;
  label: string | null;
}

/** Listed enrollment token — never contains the raw secret. */
export interface EnrollmentToken {
  id: string;
  label: string | null;
  expires_at: string;
  used_at: string | null;
  revoked_at: string | null;
  machine_id: string | null;
  created_at: string;
  [key: string]: unknown;
}

/** Derived lifecycle status for an enrollment token (computed client-side). */
export type EnrollmentTokenStatus = "active" | "used" | "expired" | "revoked";

export interface CreateEnrollmentTokenInput {
  label?: string;
}

export interface MachineHardware {
  gpu_model: string | null;
  gpu_count: number;
  vram_mb: number;
  cpu_model: string;
  cpu_cores: number;
  ram_mb: number;
  disk_gb: number;
  cuda_version: string | null;
  [key: string]: unknown;
}

export type MachineStatus = "online" | "offline" | "maintenance";
export type MachineAvailability = "online" | "offline" | "maintenance";

export interface Machine {
  machine_id: string;
  hostname: string;
  agent_version: string;
  status: MachineStatus;
  availability: MachineAvailability;
  /** Capability honesty: always "detected" — the UI shows "Detected", never "Verified". */
  verification_status: string;
  asking_price_per_hour: string | null;
  max_concurrent_jobs: number;
  region: string | null;
  last_heartbeat_at: string | null;
  hardware: MachineHardware;
  supported_workloads: string[];
  supported_isolation_tiers?: string[];
  created_at: string;
  /** Present on GET /provider/machines/{id}. */
  last_health?: unknown;
  [key: string]: unknown;
}

export interface UpdateMachineInput {
  availability?: MachineAvailability;
  asking_price_per_hour?: string;
  max_concurrent_jobs?: number;
  region?: string;
  supported_workloads?: string[];
}

// ---------------------------------------------------------------------------
// Phase 3 — Marketplace search, quotes & reservations.
// Shapes mirror docs/api/phase3-marketplace-protocol.md exactly.
// MONEY IS ALWAYS A DECIMAL STRING — never parseFloat for display.
// ---------------------------------------------------------------------------

/** Honest reliability block — success_rate is null until job history exists. */
export interface MachineReliability {
  completed_jobs: number;
  success_rate: number | null;
}

/** A single machine as exposed by the PUBLIC marketplace search. */
export interface MarketplaceMachine {
  machine_id: string;
  gpu_model: string | null;
  gpu_count: number;
  vram_mb: number;
  cpu_model: string;
  cpu_cores: number;
  ram_mb: number;
  region: string | null;
  /** Capability honesty: "detected" — UI shows "Detected", never "Verified". */
  verification_status: string;
  /** Decimal string (e.g. "1.50"). Display verbatim — do NOT parseFloat. */
  price_per_hour: string;
  currency: string;
  supported_workloads: string[];
  available_slots: number;
  max_concurrent_jobs: number;
  reliability: MachineReliability;
  last_heartbeat_at: string | null;
  [key: string]: unknown;
}

export interface MarketplaceSearchParams {
  gpu_model?: string;
  min_vram_mb?: number;
  max_price_per_hour?: string;
  region?: string;
  workload?: string;
  verification_status?: string;
  page?: number;
  page_size?: number;
}

export interface MarketplaceSearchResult {
  items: MarketplaceMachine[];
  page: number;
  page_size: number;
  total: number;
}

/** Itemized quote cost breakdown — every value is a decimal STRING. */
export interface QuoteBreakdown {
  price_per_hour: string;
  estimated_hours: string;
  gpu_cost: string;
  platform_fee_rate: string;
  platform_fee: string;
  total: string;
}

export type QuoteStatus = "active" | "expired" | "reserved" | "cancelled";

export interface Quote {
  quote_id: string;
  machine_id: string;
  customer_id: string;
  workload_type: string;
  gpu_count: number;
  estimated_minutes: number;
  currency: string;
  breakdown: QuoteBreakdown;
  max_authorized_spend: string | null;
  status: QuoteStatus;
  expires_at: string;
  created_at: string;
  [key: string]: unknown;
}

export interface CreateQuoteInput {
  machine_id: string;
  workload_type: string;
  estimated_minutes: number;
  gpu_count: number;
  /** Optional spend ceiling as a decimal string (e.g. "5.00"). */
  max_authorized_spend?: string;
}

export type ReservationStatus = "active" | "released" | "expired" | "consumed";

export interface Reservation {
  reservation_id: string;
  quote_id: string;
  machine_id: string;
  slots: number;
  status: ReservationStatus;
  expires_at: string;
  created_at: string;
  released_at?: string | null;
  [key: string]: unknown;
}

export interface ReleaseReservationResult {
  reservation_id: string;
  status: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Phase 4 — Job execution: FSM, scheduling, agent dispatch, artifacts.
// Shapes mirror docs/api/phase4-execution-protocol.md exactly.
// MONEY (max_cost, final_cost) IS ALWAYS A DECIMAL STRING — never parseFloat.
// ---------------------------------------------------------------------------

/** Every FSM state a job can occupy (lowercase, as the API reports them). */
export type JobStatus =
  | "created"
  | "validating"
  | "queued"
  | "matched"
  | "reserved"
  | "running"
  | "verifying"
  | "completed"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "retry_pending"
  | "disputed";

/** MVP workloads only. */
export type JobWorkloadType = "text-generation" | "batch-inference";

/** Honesty: "simulated" output is a deterministic transform, never real inference. */
export type JobExecutionMode = "simulated" | "hardware";

/** Terminal states — no further transitions, polling can stop. */
export const TERMINAL_JOB_STATES: readonly JobStatus[] = [
  "completed",
  "failed",
  "timed_out",
  "cancelled",
  "disputed",
];

export function isTerminalJobStatus(status: JobStatus): boolean {
  return TERMINAL_JOB_STATES.includes(status);
}

/** Explainable scheduling: the chosen machine + transparent score factors. */
export interface JobMatchExplanation {
  machine_id?: string;
  score?: number | string;
  factors?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface Job {
  /** Primary identifier returned by the live API. */
  job_id: string;
  /** Some older endpoints may echo `id`; kept for tolerance only. */
  id?: string;
  customer_id?: string;
  project_id?: string | null;
  workload_type: string;
  model: string;
  input?: Record<string, unknown>;
  status: JobStatus;
  machine_id?: string | null;
  reservation_id?: string | null;
  gpu_count?: number;
  estimated_minutes?: number;
  /** Decimal string budget cap (e.g. "5.00"). Display verbatim. */
  max_cost?: string | null;
  max_runtime_seconds?: number;
  /** "simulated" | "hardware" — MUST be surfaced wherever output is shown. */
  execution_mode: JobExecutionMode;
  /** Workload isolation tier ("container" | "gvisor" | "microvm" | "confidential"). */
  isolation_tier?: "container" | "gvisor" | "microvm" | "confidential" | string;
  match_explanation?: JobMatchExplanation | null;
  verification?: Record<string, unknown> | null;
  /** Decimal string; null in Phase 4 (nothing is charged yet). */
  final_cost?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  [key: string]: unknown;
}

/** One immutable FSM transition row (append-only job_events). */
export interface JobEvent {
  job_id?: string;
  seq: number;
  from_state: string | null;
  to_state: string;
  reason?: string | null;
  actor?: "customer" | "scheduler" | "agent" | "system" | string;
  created_at: string;
  [key: string]: unknown;
}

export interface JobArtifact {
  artifact_id: string;
  kind: string;
  size: number;
  sha256: string;
  content_type: string;
  created_at: string;
  [key: string]: unknown;
}

/** Short-lived signed download for an artifact (owner only). */
export interface ArtifactDownload {
  url: string;
  expires_at?: string;
  [key: string]: unknown;
}

/** A single log line — the API may return bare strings or structured rows. */
export type LogLine =
  | string
  | {
      ts?: string;
      timestamp?: string;
      level?: string;
      line?: string;
      message?: string;
      [key: string]: unknown;
    };

/** Logs endpoint response — tolerate an array or an envelope with `lines`. */
export type JobLogs =
  | LogLine[]
  | { lines?: LogLine[]; [key: string]: unknown };

/** Acknowledgement returned by POST /v1/jobs/{id}/cancel. */
export interface CancelAck {
  job_id?: string;
  status?: string;
  [key: string]: unknown;
}

export interface CreateJobInput {
  workload_type: JobWorkloadType | string;
  model: string;
  /** text-generation -> {prompt, max_tokens?}; batch-inference -> {items:[...]}. */
  input: Record<string, unknown>;
  /** Optional target machine; omit to let the scheduler pick. */
  machine_id?: string;
  gpu_count: number;
  estimated_minutes: number;
  /** Budget cap as a decimal string (e.g. "5.00"), validated against the quote. */
  max_cost: string;
  max_runtime_seconds: number;
  retry_policy?: { max_retries: number };
  isolation_tier?: "container" | "gvisor" | "microvm" | "confidential";
}

// ---------------------------------------------------------------------------
// Phase 5 — Billing, ledger, invoices, usage, earnings, payouts, disputes.
// Shapes mirror docs/api/phase5-billing-protocol.md exactly.
// MONEY IS ALWAYS A DECIMAL STRING (minor units internally) — never parseFloat
// for display. currency "USDC_CREDITS" is SIMULATED test credits.
// ---------------------------------------------------------------------------

/** GET /v1/billing/account — available = spendable, reserved = holds, balance = total. */
export interface BillingAccount {
  currency: string;
  /** Spendable credits (customer_credits balance). Decimal string. */
  available: string;
  /** Current holds attributable to the customer. Decimal string. */
  reserved: string;
  /** available + reserved (total owned). Decimal string. */
  balance: string;
  [key: string]: unknown;
}

export type LedgerDirection = "debit" | "credit";

/** One append-only ledger entry (the customer's own). Amount is a decimal string. */
export interface LedgerEntry {
  entry_id: string;
  transaction_id: string;
  kind: string;
  direction: LedgerDirection;
  amount: string;
  currency: string;
  reference: string | null;
  created_at: string;
  [key: string]: unknown;
}

/** One line on an itemized invoice. Amount is a decimal string. */
export interface InvoiceLineItem {
  label: string;
  amount: string;
  [key: string]: unknown;
}

export type InvoiceStatus =
  | "paid"
  | "settled"
  | "open"
  | "not_charged"
  | "refunded"
  | string;

/** An itemized invoice per settled job. All money values are decimal strings. */
export interface Invoice {
  invoice_id: string;
  job_id: string;
  line_items?: InvoiceLineItem[];
  estimated_total: string;
  actual_total: string;
  platform_fee: string;
  status: InvoiceStatus;
  currency?: string;
  created_at: string;
  [key: string]: unknown;
}

/** One metered usage record (estimated vs actual kept separate). */
export interface UsageRecord {
  usage_id?: string;
  job_id: string;
  duration_seconds?: number | null;
  estimated_cost?: string | null;
  actual_cost?: string | null;
  currency?: string;
  created_at?: string;
  [key: string]: unknown;
}

/** Spend summary returned alongside usage records. Money values are strings. */
export interface UsageSummary {
  total_spend?: string;
  total_estimated?: string;
  total_actual?: string;
  job_count?: number;
  currency?: string;
  [key: string]: unknown;
}

/** GET /v1/usage — spend summary + per-job usage records. */
export interface UsageResponse {
  summary?: UsageSummary;
  usage_records?: UsageRecord[];
  records?: UsageRecord[];
  currency?: string;
  [key: string]: unknown;
}

export type DisputeStatus =
  | "open"
  | "under_review"
  | "resolved"
  | "rejected"
  | string;

/** A dispute opened on a settled job. */
export interface Dispute {
  dispute_id: string;
  job_id: string;
  reason: string;
  status: DisputeStatus;
  resolution?: string | null;
  refund_amount?: string | null;
  created_at: string;
  resolved_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
}

/** One recent earnings entry on the provider earnings view. */
export interface EarningEntry {
  entry_id?: string;
  job_id?: string;
  kind?: string;
  direction?: LedgerDirection;
  amount: string;
  currency?: string;
  reference?: string | null;
  created_at?: string;
  [key: string]: unknown;
}

/** GET /v1/provider/earnings — LIVE accrued earnings from the ledger. */
export interface ProviderEarnings {
  available: string;
  lifetime?: string;
  currency: string;
  recent?: EarningEntry[];
  [key: string]: unknown;
}

export type PayoutStatus =
  | "requested"
  | "pending"
  | "authorized"
  | "settled"
  | "paid"
  | "rejected"
  | string;

/** A provider payout request / settlement. Amount is a decimal string. */
export interface Payout {
  payout_id: string;
  amount: string;
  currency?: string;
  status: PayoutStatus;
  created_at: string;
  settled_at?: string | null;
  authorized_at?: string | null;
  [key: string]: unknown;
}

/** One account row in the admin reconciliation report. */
export interface ReconciliationAccount {
  account_id?: string;
  owner_type?: string;
  type?: string;
  currency?: string;
  balance?: string;
  [key: string]: unknown;
}

/** GET /v1/admin/reconciliation — INVARIANT-2 proof surfaced. */
export interface Reconciliation {
  balanced: boolean;
  imbalance: string;
  total_debits: string;
  total_credits: string;
  accounts?: ReconciliationAccount[];
  [key: string]: unknown;
}

export interface PurchaseCreditsInput {
  /** Decimal string (e.g. "50.00"). */
  amount: string;
}

export interface RequestPayoutInput {
  /** Decimal string (e.g. "25.00"). */
  amount: string;
}

export interface OpenDisputeInput {
  reason: string;
}

export interface RefundInput {
  job_id?: string;
  invoice_id?: string;
  /** Decimal string. */
  amount: string;
  reason: string;
}

export interface ResolveDisputeInput {
  resolution: string;
  /** Optional decimal string. */
  refund_amount?: string;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at?: string | null;
  [key: string]: unknown;
}

export interface ApiKeyCreated extends ApiKey {
  api_key: string;
}

export interface AdminUser {
  user_id: string;
  email: string;
  status: string;
  roles: string[];
  email_verified: boolean;
  created_at: string;
  [key: string]: unknown;
}

export interface AdminUserAction {
  user_id: string;
  email: string;
  status: string;
  message: string;
}

export interface AdminAuditLog {
  id: string;
  actor_user_id?: string | null;
  action: string;
  resource_type?: string | null;
  resource_id?: string | null;
  ip?: string | null;
  reason?: string | null;
  outcome: string;
  created_at: string;
  [key: string]: unknown;
}

export interface AdminJob {
  job_id: string;
  customer_id: string;
  workload_type: string;
  model: string;
  status: JobStatus;
  execution_mode: string;
  isolation_tier?: string;
  created_at: string;
  final_cost?: string | null;
  error_code?: string | null;
  [key: string]: unknown;
}

export interface AdminPlatformStats {
  total_users: number;
  active_providers: number;
  total_jobs: number;
  completed_jobs: number;
  success_rate: number;
  total_volume: string;
  currency: string;
}

// ---------------------------------------------------------------------------
// Phase 9 — Telemetry & Time-series at scale
// ---------------------------------------------------------------------------
export interface MetricBucketPoint {
  timestamp: string;
  avg_gpu_util_pct?: number | null;
  max_gpu_util_pct?: number | null;
  avg_temp_c?: number | null;
  max_temp_c?: number | null;
  avg_mem_used_mb?: number | null;
  avg_power_w?: number | null;
  samples: number;
}

export interface MachineTelemetryCurrent {
  timestamp?: string | null;
  gpu_index?: number | null;
  gpu_util_pct?: number | null;
  mem_util_pct?: number | null;
  mem_used_mb?: number | null;
  mem_total_mb?: number | null;
  temp_c?: number | null;
  power_w?: number | null;
  [key: string]: unknown;
}

export interface MachineTelemetryResponse {
  machine_id: string;
  start_time: string;
  end_time: string;
  bucket_seconds: number;
  points: MetricBucketPoint[];
  current?: MachineTelemetryCurrent | null;
}

export interface JobMetricBucketPoint {
  timestamp: string;
  avg_gpu_util_pct?: number | null;
  avg_tokens_per_sec?: number | null;
  max_tokens_per_sec?: number | null;
  avg_mem_used_mb?: number | null;
  samples: number;
}

export interface JobTelemetryResponse {
  job_id: string;
  points: JobMetricBucketPoint[];
  total_samples: number;
  avg_tokens_per_sec?: number | null;
}

export interface PlatformTelemetryStats {
  active_gpus: number;
  fleet_avg_gpu_util_pct: number;
  fleet_avg_temp_c: number;
  fleet_total_power_kw: number;
  active_alerts_count: number;
  critical_alerts_count: number;
}

export interface TelemetryAlert {
  id: string;
  machine_id?: string | null;
  job_id?: string | null;
  alert_type: string;
  severity: "info" | "warning" | "critical" | string;
  message: string;
  metric_value?: number | null;
  threshold?: number | null;
  status: "active" | "acknowledged" | "resolved" | string;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Phase 10 — On-chain settlement & smart contracts
// ---------------------------------------------------------------------------

export interface ContractDeploymentInfo {
  chain_id: number;
  network_name: string;
  token_address: string;
  token_symbol: string;
  token_name: string;
  token_total_supply: string;
  settlement_contract_address: string;
  platform_signer_address: string;
  treasury_address: string;
  is_paused: boolean;
}

export interface EscrowResponse {
  id: string;
  job_id: string;
  customer_id: string;
  provider_id: string;
  chain_id: number;
  contract_address: string;
  token_address: string;
  job_bytes32: string;
  locked_amount_units: string;
  deadline: string;
  status: "locked" | "settled" | "refunded" | "expired" | string;
  lock_tx_hash: string;
  settle_tx_hash: string | null;
  refund_tx_hash: string | null;
  final_cost_units: string | null;
  platform_fee_units: string | null;
  voucher_signature: string | null;
  reconciled: boolean;
  settled_at: string | null;
  created_at: string;
}

export interface SettlementVoucherResponse {
  job_id: string;
  job_bytes32: string;
  final_cost_units: string;
  platform_fee_units: string;
  chain_id: number;
  contract_address: string;
  signer_address: string;
  signature: string;
}

export interface OnChainTransactionResponse {
  id: string;
  chain_id: number;
  tx_hash: string;
  tx_type: string;
  sender_address: string;
  recipient_address: string | null;
  amount_units: string | null;
  token_symbol: string;
  block_number: number | null;
  status: string;
  reconciled_with_ledger: boolean;
  ledger_txn_id: string | null;
  job_id: string | null;
  created_at: string;
}

export interface OnChainReconciliationSummary {
  total_escrows: number;
  active_locked_amount_units: string;
  total_settled_amount_units: string;
  total_fees_collected_units: string;
  total_refunded_amount_units: string;
  unreconciled_transactions_count: number;
  ledger_parity_verified: boolean;
}

/** Normalized API error surfaced to the UI layer. */
export class ApiError extends Error {
  status: number;
  details?: unknown;

  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

export interface CurrentUser {
  id: string;
  email: string;
  email_verified: boolean;
  status: string;
  roles: string[];
}

function getCsrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  token?: string | null;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, token, headers, method = "GET", ...rest } = options;

  const finalHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    ...(headers as Record<string, string> | undefined),
  };

  // Only pass Bearer header if a real token is provided (not our cookie-session sentinel)
  if (token && token !== "cookie-session") {
    finalHeaders.Authorization = `Bearer ${token}`;
  }

  // Double-submit CSRF protection on mutating requests
  const upperMethod = method.toUpperCase();
  if (upperMethod !== "GET" && upperMethod !== "HEAD" && !finalHeaders["X-CSRF-Token"]) {
    const csrf = getCsrfToken();
    if (csrf) {
      finalHeaders["X-CSRF-Token"] = csrf;
    }
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      ...rest,
      method,
      credentials: "include", // Send and store httpOnly cookies across requests
      headers: finalHeaders,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    // Network-level failure (backend down, CORS, offline).
    throw new ApiError(
      "Unable to reach the ComputeCoin API. Is the backend running?",
      0,
      err,
    );
  }

  const isJson = res.headers
    .get("content-type")
    ?.toLowerCase()
    .includes("application/json");
  const payload = isJson ? await res.json().catch(() => null) : await res.text();

  if (!res.ok) {
    const message =
      (isJson &&
        payload &&
        typeof payload === "object" &&
        "detail" in payload &&
        typeof (payload as { detail: unknown }).detail === "string" &&
        (payload as { detail: string }).detail) ||
      `Request failed with status ${res.status}`;
    throw new ApiError(message, res.status, payload);
  }

  return payload as T;
}

export const api = {
  register(credentials: AuthCredentials): Promise<RegisteredUser> {
    return request<RegisteredUser>("/auth/register", {
      method: "POST",
      body: credentials,
    });
  },

  login(credentials: AuthCredentials): Promise<TokenPair> {
    return request<TokenPair>("/auth/login", {
      method: "POST",
      body: credentials,
    });
  },

  refresh(refreshToken?: string): Promise<TokenPair> {
    return request<TokenPair>("/auth/refresh", {
      method: "POST",
      body: refreshToken ? { refresh_token: refreshToken } : undefined,
    });
  },

  getMe(): Promise<CurrentUser> {
    return request<CurrentUser>("/auth/me", {
      method: "GET",
    });
  },

  logout(): Promise<{ message: string }> {
    return request<{ message: string }>("/auth/logout", {
      method: "POST",
    });
  },

  getProjects(accessToken: string): Promise<Project[]> {
    return request<Project[]>("/projects", {
      method: "GET",
      token: accessToken,
    });
  },

  // -------------------------------------------------------------------------
  // Phase 2 — Provider management endpoints (all carry the user access token).
  // -------------------------------------------------------------------------

  /** POST /provider/register — make the current user a provider (idempotent). */
  providerRegister(
    accessToken: string,
    input: ProviderRegisterInput = {},
  ): Promise<Provider> {
    return request<Provider>("/provider/register", {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  /** GET /provider/enrollment-tokens — list own tokens (never the raw secret). */
  listEnrollmentTokens(accessToken: string): Promise<EnrollmentToken[]> {
    return request<EnrollmentToken[]>("/provider/enrollment-tokens", {
      method: "GET",
      token: accessToken,
    });
  },

  /** POST /provider/enrollment-tokens — mint a token; raw value returned ONCE. */
  createEnrollmentToken(
    accessToken: string,
    input: CreateEnrollmentTokenInput = {},
  ): Promise<EnrollmentTokenCreated> {
    return request<EnrollmentTokenCreated>("/provider/enrollment-tokens", {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  /** DELETE /provider/enrollment-tokens/{id} — revoke an unused token. */
  revokeEnrollmentToken(accessToken: string, id: string): Promise<void> {
    return request<void>(`/provider/enrollment-tokens/${id}`, {
      method: "DELETE",
      token: accessToken,
    });
  },

  /** GET /provider/machines — list the provider's own machines. */
  listMachines(accessToken: string): Promise<Machine[]> {
    return request<Machine[]>("/provider/machines", {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /provider/machines/{id} — single owned machine (+ last_health). */
  getMachine(accessToken: string, id: string): Promise<Machine> {
    return request<Machine>(`/provider/machines/${id}`, {
      method: "GET",
      token: accessToken,
    });
  },

  /** PATCH /provider/machines/{id} — update operational settings. */
  updateMachine(
    accessToken: string,
    id: string,
    patch: UpdateMachineInput,
  ): Promise<Machine> {
    return request<Machine>(`/provider/machines/${id}`, {
      method: "PATCH",
      token: accessToken,
      body: patch,
    });
  },

  /** DELETE /provider/machines/{id} — deregister the machine. */
  deregisterMachine(accessToken: string, id: string): Promise<void> {
    return request<void>(`/provider/machines/${id}`, {
      method: "DELETE",
      token: accessToken,
    });
  },

  // -------------------------------------------------------------------------
  // Phase 3 — Marketplace search (PUBLIC, no auth).
  // -------------------------------------------------------------------------

  /** GET /v1/marketplace/machines — public search over live, priced machines. */
  searchMachines(
    params: MarketplaceSearchParams = {},
  ): Promise<MarketplaceSearchResult> {
    const qs = new URLSearchParams();
    if (params.gpu_model) qs.set("gpu_model", params.gpu_model);
    if (params.min_vram_mb != null)
      qs.set("min_vram_mb", String(params.min_vram_mb));
    if (params.max_price_per_hour)
      qs.set("max_price_per_hour", params.max_price_per_hour);
    if (params.region) qs.set("region", params.region);
    if (params.workload) qs.set("workload", params.workload);
    if (params.verification_status)
      qs.set("verification_status", params.verification_status);
    if (params.page != null) qs.set("page", String(params.page));
    if (params.page_size != null) qs.set("page_size", String(params.page_size));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<MarketplaceSearchResult>(
      `/v1/marketplace/machines${suffix}`,
      { method: "GET" },
    );
  },

  /** GET /v1/marketplace/machines/{id} — single listable machine (public). */
  getMarketplaceMachine(id: string): Promise<MarketplaceMachine> {
    return request<MarketplaceMachine>(`/v1/marketplace/machines/${id}`, {
      method: "GET",
    });
  },

  // -------------------------------------------------------------------------
  // Phase 3 — Quotes & reservations (customer JWT; owner-scoped server-side).
  // -------------------------------------------------------------------------

  /** POST /v1/quotes — snapshot an itemized, time-limited cost estimate. */
  createQuote(accessToken: string, input: CreateQuoteInput): Promise<Quote> {
    return request<Quote>("/v1/quotes", {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  /** GET /v1/quotes — the customer's own quotes. */
  listQuotes(accessToken: string): Promise<Quote[]> {
    return request<Quote[]>("/v1/quotes", {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /v1/quotes/{id} — a single owned quote (404 for others'). */
  getQuote(accessToken: string, id: string): Promise<Quote> {
    return request<Quote>(`/v1/quotes/${id}`, {
      method: "GET",
      token: accessToken,
    });
  },

  /** POST /v1/quotes/{id}/reserve — atomically hold a capacity slot. */
  reserveQuote(accessToken: string, id: string): Promise<Reservation> {
    return request<Reservation>(`/v1/quotes/${id}/reserve`, {
      method: "POST",
      token: accessToken,
      body: {},
    });
  },

  /** GET /v1/reservations — the customer's own reservations. */
  listReservations(accessToken: string): Promise<Reservation[]> {
    return request<Reservation[]>("/v1/reservations", {
      method: "GET",
      token: accessToken,
    });
  },

  /** POST /v1/reservations/{id}/release — free the held slot (idempotent-ish). */
  releaseReservation(
    accessToken: string,
    id: string,
  ): Promise<ReleaseReservationResult> {
    return request<ReleaseReservationResult>(
      `/v1/reservations/${id}/release`,
      { method: "POST", token: accessToken, body: {} },
    );
  },

  // -------------------------------------------------------------------------
  // Phase 4 — Jobs (customer JWT; owner-scoped server-side, others -> 404).
  // -------------------------------------------------------------------------

  /**
   * POST /v1/jobs — submit a job. Pass an Idempotency-Key so a retried submit
   * returns the SAME job instead of creating a duplicate.
   */
  createJob(
    accessToken: string,
    input: CreateJobInput,
    idempotencyKey?: string,
  ): Promise<Job> {
    const headers: Record<string, string> = {};
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    return request<Job>("/v1/jobs", {
      method: "POST",
      token: accessToken,
      body: input,
      headers,
    });
  },

  /** GET /v1/jobs — the customer's own jobs (tolerates array or {items}). */
  async listJobs(accessToken: string): Promise<Job[]> {
    const data = await request<Job[] | { items?: Job[] }>("/v1/jobs", {
      method: "GET",
      token: accessToken,
    });
    if (Array.isArray(data)) return data;
    return data?.items ?? [];
  },

  /** GET /v1/jobs/{id} — a single owned job (404 for others'). */
  getJob(accessToken: string, id: string): Promise<Job> {
    return request<Job>(`/v1/jobs/${id}`, {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /v1/jobs/{id}/events — ordered FSM history (tolerates array or {events}). */
  async getJobEvents(accessToken: string, id: string): Promise<JobEvent[]> {
    const data = await request<JobEvent[] | { events?: JobEvent[] }>(
      `/v1/jobs/${id}/events`,
      { method: "GET", token: accessToken },
    );
    if (Array.isArray(data)) return data;
    return data?.events ?? [];
  },

  /** GET /v1/jobs/{id}/logs — log lines (array or {lines} envelope). */
  getJobLogs(accessToken: string, id: string): Promise<JobLogs> {
    return request<JobLogs>(`/v1/jobs/${id}/logs`, {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /v1/jobs/{id}/artifacts — output artifacts (array or {artifacts}). */
  async getJobArtifacts(
    accessToken: string,
    id: string,
  ): Promise<JobArtifact[]> {
    const data = await request<JobArtifact[] | { artifacts?: JobArtifact[] }>(
      `/v1/jobs/${id}/artifacts`,
      { method: "GET", token: accessToken },
    );
    if (Array.isArray(data)) return data;
    return data?.artifacts ?? [];
  },

  /** GET /v1/jobs/{id}/artifacts/{aid}/download — short-lived signed URL. */
  getArtifactDownloadUrl(
    accessToken: string,
    id: string,
    artifactId: string,
  ): Promise<ArtifactDownload> {
    return request<ArtifactDownload>(
      `/v1/jobs/${id}/artifacts/${artifactId}/download`,
      { method: "GET", token: accessToken },
    );
  },

  /** POST /v1/jobs/{id}/cancel — request cancellation (honored at next safe point). */
  cancelJob(accessToken: string, id: string): Promise<CancelAck> {
    return request<CancelAck>(`/v1/jobs/${id}/cancel`, {
      method: "POST",
      token: accessToken,
      body: {},
    });
  },

  // -------------------------------------------------------------------------
  // Phase 5 — Billing (customer JWT; owner-scoped server-side).
  // -------------------------------------------------------------------------

  /** GET /v1/billing/account — live balance (available / reserved / total). */
  getBillingAccount(accessToken: string): Promise<BillingAccount> {
    return request<BillingAccount>("/v1/billing/account", {
      method: "GET",
      token: accessToken,
    });
  },

  /** POST /v1/billing/credits/purchase — simulated top-up; returns new account. */
  purchaseCredits(
    accessToken: string,
    input: PurchaseCreditsInput,
  ): Promise<BillingAccount> {
    return request<BillingAccount>("/v1/billing/credits/purchase", {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  /** GET /v1/billing/ledger — the customer's own ledger entries (array or {items}/{entries}). */
  async getLedger(accessToken: string): Promise<LedgerEntry[]> {
    const data = await request<
      LedgerEntry[] | { items?: LedgerEntry[]; entries?: LedgerEntry[] }
    >("/v1/billing/ledger", { method: "GET", token: accessToken });
    if (Array.isArray(data)) return data;
    return data?.items ?? data?.entries ?? [];
  },

  /** GET /v1/usage — spend summary + per-job usage records. */
  getUsage(accessToken: string): Promise<UsageResponse> {
    return request<UsageResponse>("/v1/usage", {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /v1/invoices — the customer's own invoices (array or {items}/{invoices}). */
  async listInvoices(accessToken: string): Promise<Invoice[]> {
    const data = await request<
      Invoice[] | { items?: Invoice[]; invoices?: Invoice[] }
    >("/v1/invoices", { method: "GET", token: accessToken });
    if (Array.isArray(data)) return data;
    return data?.items ?? data?.invoices ?? [];
  },

  /** GET /v1/invoices/{id} — a single owned invoice (404 for others'). */
  getInvoice(accessToken: string, id: string): Promise<Invoice> {
    return request<Invoice>(`/v1/invoices/${id}`, {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /v1/invoices/{id}/download — signed URL or inline document. */
  getInvoiceDownloadUrl(
    accessToken: string,
    id: string,
  ): Promise<ArtifactDownload> {
    return request<ArtifactDownload>(`/v1/invoices/${id}/download`, {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /v1/disputes — the customer's own disputes (array or {items}/{disputes}). */
  async listDisputes(accessToken: string): Promise<Dispute[]> {
    const data = await request<
      Dispute[] | { items?: Dispute[]; disputes?: Dispute[] }
    >("/v1/disputes", { method: "GET", token: accessToken });
    if (Array.isArray(data)) return data;
    return data?.items ?? data?.disputes ?? [];
  },

  /** GET /v1/disputes/{id} — a single owned dispute. */
  getDispute(accessToken: string, id: string): Promise<Dispute> {
    return request<Dispute>(`/v1/disputes/${id}`, {
      method: "GET",
      token: accessToken,
    });
  },

  /** POST /v1/jobs/{job_id}/dispute — open a dispute on a settled job. */
  openDispute(
    accessToken: string,
    jobId: string,
    input: OpenDisputeInput,
  ): Promise<Dispute> {
    return request<Dispute>(`/v1/jobs/${jobId}/dispute`, {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  // -------------------------------------------------------------------------
  // Phase 5 — Provider earnings & payouts (provider JWT).
  // -------------------------------------------------------------------------

  /** GET /v1/provider/earnings — LIVE accrued earnings from the ledger. */
  getProviderEarnings(accessToken: string): Promise<ProviderEarnings> {
    return request<ProviderEarnings>("/v1/provider/earnings", {
      method: "GET",
      token: accessToken,
    });
  },

  /** GET /v1/provider/payouts — the provider's own payouts (array or {items}/{payouts}). */
  async listPayouts(accessToken: string): Promise<Payout[]> {
    const data = await request<
      Payout[] | { items?: Payout[]; payouts?: Payout[] }
    >("/v1/provider/payouts", { method: "GET", token: accessToken });
    if (Array.isArray(data)) return data;
    return data?.items ?? data?.payouts ?? [];
  },

  /** POST /v1/provider/payouts — request a payout. 409 insufficient_earnings if too large. */
  requestPayout(
    accessToken: string,
    input: RequestPayoutInput,
  ): Promise<Payout> {
    return request<Payout>("/v1/provider/payouts", {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  // -------------------------------------------------------------------------
  // Phase 5 — Admin / operator (JWT; RBAC enforced server-side).
  // -------------------------------------------------------------------------

  /** GET /v1/admin/reconciliation — INVARIANT-2 proof (balanced + imbalance + totals). */
  getReconciliation(accessToken: string): Promise<Reconciliation> {
    return request<Reconciliation>("/v1/admin/reconciliation", {
      method: "GET",
      token: accessToken,
    });
  },

  /** POST /v1/admin/refunds — authorize an audited refund that reverses a charge. */
  postRefund(accessToken: string, input: RefundInput): Promise<unknown> {
    return request<unknown>("/v1/admin/refunds", {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  /** POST /v1/admin/payouts/{id}/authorize — settle a payout (authorized role only). */
  authorizePayout(accessToken: string, id: string): Promise<Payout> {
    return request<Payout>(`/v1/admin/payouts/${id}/authorize`, {
      method: "POST",
      token: accessToken,
      body: {},
    });
  },

  /** POST /v1/admin/disputes/{id}/resolve — resolve a dispute (audited). */
  resolveDispute(
    accessToken: string,
    id: string,
    input: ResolveDisputeInput,
  ): Promise<Dispute> {
    return request<Dispute>(`/v1/admin/disputes/${id}/resolve`, {
      method: "POST",
      token: accessToken,
      body: input,
    });
  },

  // --- Projects & API keys (Phase 7) ---
  createProject(accessToken: string, name: string, description?: string): Promise<Project> {
    return request<Project>("/v1/projects", {
      method: "POST",
      token: accessToken,
      body: { name, description },
    });
  },

  deleteProject(accessToken: string, projectId: string): Promise<void> {
    return request<void>(`/v1/projects/${projectId}`, {
      method: "DELETE",
      token: accessToken,
    });
  },

  listApiKeys(accessToken: string, projectId: string): Promise<ApiKey[]> {
    return request<ApiKey[]>(`/v1/projects/${projectId}/api-keys`, {
      method: "GET",
      token: accessToken,
    });
  },

  createApiKey(accessToken: string, projectId: string, name: string): Promise<ApiKeyCreated> {
    return request<ApiKeyCreated>(`/v1/projects/${projectId}/api-keys`, {
      method: "POST",
      token: accessToken,
      body: { name },
    });
  },

  revokeApiKey(accessToken: string, projectId: string, keyId: string): Promise<void> {
    return request<void>(`/v1/projects/${projectId}/api-keys/${keyId}`, {
      method: "DELETE",
      token: accessToken,
    });
  },

  // --- Admin Operations (Phase 7) ---
  listAdminUsers(accessToken: string): Promise<AdminUser[]> {
    return request<AdminUser[]>("/v1/admin/users", {
      method: "GET",
      token: accessToken,
    });
  },

  suspendUser(accessToken: string, userId: string): Promise<AdminUserAction> {
    return request<AdminUserAction>(`/v1/admin/users/${userId}/suspend`, {
      method: "POST",
      token: accessToken,
    });
  },

  reinstateUser(accessToken: string, userId: string): Promise<AdminUserAction> {
    return request<AdminUserAction>(`/v1/admin/users/${userId}/reinstate`, {
      method: "POST",
      token: accessToken,
    });
  },

  listAdminAuditLogs(accessToken: string, limit: number = 50, offset: number = 0): Promise<AdminAuditLog[]> {
    return request<AdminAuditLog[]>(`/v1/admin/audit-logs?limit=${limit}&offset=${offset}`, {
      method: "GET",
      token: accessToken,
    });
  },

  listAllAdminJobs(accessToken: string, limit: number = 50): Promise<AdminJob[]> {
    return request<AdminJob[]>(`/v1/admin/jobs?limit=${limit}`, {
      method: "GET",
      token: accessToken,
    });
  },

  getAdminPlatformStats(accessToken: string): Promise<AdminPlatformStats> {
    return request<AdminPlatformStats>("/v1/admin/stats", {
      method: "GET",
      token: accessToken,
    });
  },

  // --- Telemetry & Operational Health (Phase 9) ---
  getMachineTelemetry(
    accessToken: string,
    machineId: string,
    bucketSeconds: number = 60
  ): Promise<MachineTelemetryResponse> {
    return request<MachineTelemetryResponse>(
      `/v1/telemetry/machines/${machineId}?bucket_seconds=${bucketSeconds}`,
      { method: "GET", token: accessToken }
    );
  },

  getJobTelemetry(
    accessToken: string,
    jobId: string,
    bucketSeconds: number = 60
  ): Promise<JobTelemetryResponse> {
    return request<JobTelemetryResponse>(
      `/v1/telemetry/jobs/${jobId}?bucket_seconds=${bucketSeconds}`,
      { method: "GET", token: accessToken }
    );
  },

  getPlatformTelemetryStats(accessToken: string): Promise<PlatformTelemetryStats> {
    return request<PlatformTelemetryStats>("/v1/telemetry/stats", {
      method: "GET",
      token: accessToken,
    });
  },

  listTelemetryAlerts(
    accessToken: string,
    status?: string,
    limit: number = 50
  ): Promise<TelemetryAlert[]> {
    const q = status ? `&status=${status}` : "";
    return request<TelemetryAlert[]>(`/v1/telemetry/alerts?limit=${limit}${q}`, {
      method: "GET",
      token: accessToken,
    });
  },

  acknowledgeAlert(accessToken: string, alertId: string): Promise<{ id: string; status: string }> {
    return request<{ id: string; status: string }>(
      `/v1/telemetry/alerts/${alertId}/acknowledge`,
      { method: "POST", token: accessToken }
    );
  },

  resolveAlert(accessToken: string, alertId: string): Promise<{ id: string; status: string }> {
    return request<{ id: string; status: string }>(
      `/v1/telemetry/alerts/${alertId}/resolve`,
      { method: "POST", token: accessToken }
    );
  },

  // -------------------------------------------------------------------------
  // Phase 10 — On-chain settlement & smart contracts
  // -------------------------------------------------------------------------

  getContractInfo(): Promise<ContractDeploymentInfo> {
    return request<ContractDeploymentInfo>("/v1/settlement/contracts");
  },

  getSettlementEscrows(
    accessToken: string,
    params?: { job_id?: string; status?: string }
  ): Promise<EscrowResponse[]> {
    const q = new URLSearchParams();
    if (params?.job_id) q.set("job_id", params.job_id);
    if (params?.status) q.set("status", params.status);
    const qs = q.toString() ? `?${q.toString()}` : "";
    return request<EscrowResponse[]>(`/v1/settlement/escrows${qs}`, {
      token: accessToken,
    });
  },

  getSettlementEscrow(accessToken: string, jobId: string): Promise<EscrowResponse> {
    return request<EscrowResponse>(`/v1/settlement/escrows/${jobId}`, {
      token: accessToken,
    });
  },

  createEscrowLock(
    accessToken: string,
    payload: {
      job_id: string;
      provider_id: string;
      locked_amount_units: string;
      deadline_seconds?: number;
      lock_tx_hash: string;
      customer_address: string;
      provider_address: string;
    }
  ): Promise<EscrowResponse> {
    return request<EscrowResponse>("/v1/settlement/escrows", {
      method: "POST",
      token: accessToken,
      body: JSON.stringify(payload),
    });
  },

  issueSettlementVoucher(
    accessToken: string,
    payload: {
      job_id: string;
      final_cost_units: string;
      platform_fee_units: string;
    }
  ): Promise<SettlementVoucherResponse> {
    return request<SettlementVoucherResponse>("/v1/settlement/vouchers", {
      method: "POST",
      token: accessToken,
      body: JSON.stringify(payload),
    });
  },

  confirmSettlement(
    accessToken: string,
    payload: {
      job_id: string;
      settle_tx_hash: string;
      block_number?: number;
    }
  ): Promise<EscrowResponse> {
    return request<EscrowResponse>("/v1/settlement/confirm", {
      method: "POST",
      token: accessToken,
      body: JSON.stringify(payload),
    });
  },

  confirmRefund(
    accessToken: string,
    payload: {
      job_id: string;
      refund_tx_hash: string;
      block_number?: number;
    }
  ): Promise<EscrowResponse> {
    return request<EscrowResponse>("/v1/settlement/refund", {
      method: "POST",
      token: accessToken,
      body: JSON.stringify(payload),
    });
  },

  getSettlementReconciliation(accessToken: string): Promise<OnChainReconciliationSummary> {
    return request<OnChainReconciliationSummary>("/v1/settlement/reconciliation", {
      token: accessToken,
    });
  },

  getSettlementTransactions(accessToken: string): Promise<OnChainTransactionResponse[]> {
    return request<OnChainTransactionResponse[]>("/v1/settlement/transactions", {
      token: accessToken,
    });
  },
};

/** Currency label — credits are simulated, never real money. */
export const CREDITS_LABEL = "USDC_CREDITS — simulated credits";

/** Pull a machine-readable error code (e.g. "no_capacity") from an ApiError. */
export function apiErrorCode(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const d = err.details;
  if (d && typeof d === "object" && "detail" in d) {
    const detail = (d as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "code" in detail) {
      const code = (detail as { code: unknown }).code;
      if (typeof code === "string") return code;
    }
  }
  return null;
}

/** Derive a token's lifecycle status from its timestamps. */
export function enrollmentTokenStatus(
  token: EnrollmentToken,
): EnrollmentTokenStatus {
  if (token.revoked_at) return "revoked";
  if (token.used_at) return "used";
  if (token.expires_at && new Date(token.expires_at).getTime() < Date.now()) {
    return "expired";
  }
  return "active";
}
