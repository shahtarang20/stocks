/** Request/response types for the ComputeCoin API (subset). */

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type?: string;
  expires_in?: number;
}

export interface Project {
  id: string;
  name: string;
  created_at?: string;
}

export interface ApiKey {
  id: string;
  /** Full key is only present once, at creation. */
  key?: string;
  prefix: string;
  name?: string;
}

export interface ChatMessage {
  role: string;
  content: string;
}

export interface ChatCompletion {
  id: string;
  object: string;
  model: string;
  choices: Array<Record<string, unknown>>;
  usage?: Record<string, unknown>;
}

export interface EmbeddingResponse {
  object: string;
  model: string;
  data: Array<Record<string, unknown>>;
  usage?: Record<string, unknown>;
}

export interface ClientOptions {
  accessToken?: string;
  apiKey?: string;
  /** Inject a custom fetch (e.g. for tests). Defaults to global fetch. */
  fetch?: typeof fetch;
}

// -- Phase 3: marketplace / quotes / reservations ------------------------- //
// Every money field below is a decimal STRING with 2 fraction digits (e.g.
// "1.50"), computed server-side with Decimal. The SDK keeps them as `string`
// and never coerces to number, so no precision is lost. Credits
// ("USDC_CREDITS") are simulated test credits in Phase 3 (no real payment rail).

export interface MachineReliability {
  completed_jobs: number;
  /** `null` until completed-job history exists -- never a fabricated rating. */
  success_rate: string | null;
}

export interface MarketplaceMachine {
  machine_id: string;
  gpu_model: string | null;
  gpu_count: number;
  vram_mb: number;
  cpu_model?: string;
  cpu_cores: number;
  ram_mb: number;
  region: string | null;
  verification_status?: string;
  /** money (decimal string) */
  price_per_hour: string;
  currency: string;
  supported_workloads: string[];
  available_slots: number;
  max_concurrent_jobs: number;
  reliability?: MachineReliability;
  last_heartbeat_at?: string;
}

export interface MachinePage {
  items: MarketplaceMachine[];
  page: number;
  page_size: number;
  total: number;
}

export interface SearchMachinesParams {
  gpu_model?: string;
  min_vram_mb?: number;
  /** money (decimal string) */
  max_price_per_hour?: string;
  region?: string;
  workload?: string;
  verification_status?: string;
  page?: number;
  page_size?: number;
}

export interface QuoteBreakdown {
  // All money / rate fields are decimal strings.
  price_per_hour: string;
  estimated_hours: string;
  gpu_cost: string;
  platform_fee_rate: string;
  platform_fee: string;
  total: string;
}

export interface Quote {
  quote_id: string;
  machine_id: string;
  customer_id: string;
  workload_type: string;
  gpu_count: number;
  estimated_minutes: number;
  currency: string;
  breakdown: QuoteBreakdown;
  /** money (decimal string) or null */
  max_authorized_spend: string | null;
  status: string;
  expires_at?: string;
  created_at?: string;
}

export interface CreateQuoteParams {
  machine_id: string;
  workload_type: string;
  estimated_minutes: number;
  gpu_count?: number;
  /** money (decimal string) */
  max_authorized_spend?: string;
}

export interface Reservation {
  reservation_id: string;
  quote_id: string;
  machine_id?: string;
  slots: number;
  status: string;
  expires_at?: string;
  created_at?: string;
  released_at?: string | null;
}

export interface ReleaseResult {
  reservation_id: string;
  status: string;
}

// -- Phase 4: jobs / events / artifacts ---------------------------------- //
// `execution_mode` is "simulated" or "hardware". The SimulatedExecutor (CPU
// dev, no GPU/Docker) produces a DETERMINISTIC transform and stamps every job
// it creates `execution_mode: "simulated"` -- it is NEVER real model inference,
// and the SDK surfaces that stamp. Money fields (max_cost, final_cost) stay
// decimal STRINGS, never coerced to number.

export type ExecutionMode = "simulated" | "hardware";

export interface RetryPolicy {
  max_retries: number;
}

export interface Job {
  id: string;
  customer_id?: string;
  project_id?: string;
  workload_type: string;
  model: string;
  input: Record<string, unknown>;
  status: string;
  /** "simulated" | "hardware" -- honesty stamp from the executor. */
  execution_mode?: ExecutionMode;
  machine_id?: string | null;
  reservation_id?: string | null;
  gpu_count: number;
  estimated_minutes?: number;
  /** money (decimal string) */
  max_cost?: string;
  max_runtime_seconds?: number;
  retry_policy?: RetryPolicy | null;
  attempts_made?: number;
  priority?: number;
  idempotency_key?: string | null;
  /** chosen machine + score factors; null until MATCHED. */
  match_explanation?: Record<string, unknown> | null;
  verification?: Record<string, unknown> | null;
  /** money (decimal string); null in Phase 4. */
  final_cost?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface CreateJobParams {
  workload_type: string;
  model: string;
  input: Record<string, unknown>;
  machine_id?: string;
  gpu_count: number;
  estimated_minutes: number;
  /** money (decimal string) */
  max_cost: string;
  max_runtime_seconds: number;
  retry_policy?: RetryPolicy;
}

/** One immutable row of the append-only job FSM history. */
export interface JobEvent {
  job_id: string;
  seq: number;
  from_state?: string | null;
  to_state: string;
  reason?: string | null;
  /** customer | scheduler | agent | system */
  actor?: string | null;
  created_at?: string;
}

export interface JobArtifact {
  artifact_id: string;
  kind?: string;
  size?: number;
  sha256?: string;
  content_type?: string;
  created_at?: string;
}

/** A short-lived signed download URL for an artifact (owner only). */
export interface ArtifactDownload {
  url: string;
  expires_at?: string;
}

export interface JobLogs {
  lines: string[];
}

/** Acknowledgement that cancellation was requested (honored at next safe point). */
export interface CancelAck {
  job_id?: string;
  status?: string;
  cancellation_requested?: boolean;
}

export interface WaitForJobOptions {
  /** seconds between polls */
  poll?: number;
  /** total seconds before giving up */
  timeout?: number;
}

// -- Phase 5: billing / ledger / invoices / disputes / payouts ------------ //
// The MONEY layer. EVERY money field below is a decimal STRING (e.g. "12.34"),
// computed server-side from integer minor units / Decimal. The SDK keeps them as
// `string` and NEVER coerces to number, so no precision is lost. Currency
// "USDC_CREDITS" is a SIMULATED test credit -- there is no real payment rail
// (PAYMENT_PROVIDER_CONFIGURATION=simulated). An estimated amount is never
// reported as a received/actual amount; the two are kept separate on invoices.

export interface BillingAccount {
  currency: string;
  /** money (decimal string): spendable balance (customer_credits). */
  available: string;
  /** money (decimal string): total of current holds attributable to the customer. */
  reserved: string;
  /** money (decimal string): total owned (available + reserved). */
  balance: string;
}

/** One immutable (append-only) row of the double-entry ledger. */
export interface LedgerEntry {
  entry_id: string;
  transaction_id: string;
  /** credit_purchase | hold | capture | release | payout_request | ... */
  kind: string;
  /** "debit" | "credit" */
  direction: string;
  /** money (decimal string), always > 0 */
  amount: string;
  currency: string;
  reference?: string | null;
  created_at?: string;
}

/** Per-job usage: duration + estimated vs actual cost (kept separate). */
export interface UsageRecord {
  job_id: string;
  duration_seconds?: number | null;
  /** money (decimal string) */
  estimated_cost?: string | null;
  /** money (decimal string) */
  actual_cost?: string | null;
  currency: string;
  created_at?: string;
}

/** Spend summary plus the per-job usage records. */
export interface Usage {
  currency: string;
  /** money (decimal string) */
  total_estimated?: string | null;
  /** money (decimal string) */
  total_actual?: string | null;
  records: UsageRecord[];
}

export interface InvoiceLineItem {
  label: string;
  /** money (decimal string) */
  amount: string;
}

/**
 * Itemized invoice per settled job. Estimated vs actual are kept separate -- an
 * estimated total is NEVER reported as the actual charged amount.
 */
export interface Invoice {
  invoice_id: string;
  job_id: string;
  line_items: InvoiceLineItem[];
  /** money (decimal string) */
  estimated_total?: string | null;
  /** money (decimal string) */
  actual_total?: string | null;
  /** money (decimal string) */
  platform_fee?: string | null;
  currency: string;
  status: string;
  created_at?: string;
}

/** A dispute opened on a settled job. */
export interface Dispute {
  dispute_id: string;
  job_id: string;
  reason?: string | null;
  /** open | resolved | ... */
  status: string;
  resolution?: string | null;
  /** money (decimal string) */
  refund_amount?: string | null;
  created_at?: string;
  resolved_at?: string | null;
}

/** LIVE accrued provider earnings derived from the ledger (never the demo). */
export interface ProviderEarnings {
  currency: string;
  /** money (decimal string): withdrawable now. */
  available: string;
  /** money (decimal string): total ever accrued. */
  lifetime: string;
  recent: LedgerEntry[];
}

/** A provider payout request against accrued earnings. */
export interface Payout {
  payout_id: string;
  /** money (decimal string) */
  amount: string;
  currency: string;
  /** requested | authorized | settled | ... */
  status: string;
  created_at?: string;
  settled_at?: string | null;
}

export interface PurchaseCreditsParams {
  /** money (decimal string) */
  amount: string;
}

export interface OpenDisputeParams {
  reason: string;
}

export interface RequestPayoutParams {
  /** money (decimal string) */
  amount: string;
}
