/**
 * Typed client for Kits (U1), the data plane (U2), Job Spec v2 submit (U7) and the optional quote (U17).
 * Everything goes through the shared `request` helper (cookie session, CSRF, ApiError).
 */

import { ApiError, apiAbsoluteUrl, request, type Job } from "@/lib/api";
import { extractServerErrors, type ServerFieldError } from "@/lib/schema-form";

// ---- Kits -----------------------------------------------------------------

export interface KitSummary {
  slug: string;
  version: string;
  name: string;
  category: string;
  status: "published" | "yanked" | string;
  image_digest: string;
  /** batch (a job), session (opened from Sessions) or service. Older servers omit it: treat as batch. */
  mode?: "batch" | "session" | "service" | string;
}

export interface KitInputSpec { name: string; kind: string; required?: boolean }
export interface KitOutputSpec { name: string; path: string }

export interface KitManifest {
  slug: string;
  version: string;
  name: string;
  description?: string;
  category: string;
  mode?: string;
  param_schema?: Record<string, unknown>;
  inputs?: KitInputSpec[];
  outputs?: KitOutputSpec[];
  resource_defaults?: {
    gpu_count?: number; min_vram_gb?: number; cpu?: number; ram_gb?: number; disk_gb?: number;
    min_compute_capability?: string;
  };
  billing_hints?: { unit?: string; est_gpu_seconds_per_unit?: number };
  verification?: { method?: string };
  [key: string]: unknown;
}

export interface KitDetail extends KitSummary {
  manifest: KitManifest;
  manifest_sha256: string;
  signature: string;
  signing_key_id: string;
}

// ---- Job Spec v2 ----------------------------------------------------------

export interface JobSpecV2 {
  version: 2;
  kit: string;
  mode: "batch";
  inputs: { name: string; source: string }[];
  outputs: { name: string; stream_previews?: boolean }[];
  params: Record<string, unknown>;
  resources?: { gpu?: { count?: number; min_vram_gb?: number } };
  billing?: { unit?: string; expected_units?: number; max_cost?: string };
}

export interface JobV2CreateInput {
  spec: JobSpecV2;
  project_id?: string | null;
  isolation_tier?: "container" | "open";
  required_gpu_runtime?: "cuda" | "rocm" | "any";
}

export interface JobV2 extends Job {
  kit?: string | null;
  spec_v2?: Record<string, unknown> | null;
}

export interface ValidateSpecResult {
  valid: boolean;
  kit: string;
  effective_resources: Record<string, unknown>;
}

// ---- Data plane -----------------------------------------------------------

export interface DataFile {
  id: string;
  name: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
  expires_at: string | null;
}

export interface DataUpload {
  id: string;
  status: "active" | "complete" | "failed" | "aborted" | string;
  size_bytes: number;
  sha256: string;
  part_size: number;
  part_count: number;
  expires_at: string;
  error_code: string | null;
  deduplicated: boolean;
  file: DataFile | null;
}

export interface UploadState {
  upload: DataUpload;
  verified_parts: number[];
  corrupt_parts: number[];
  pending_parts: number[];
  missing_parts: number[];
}

export interface PartUrl {
  part_number: number;
  already_uploaded: boolean;
  method?: "PUT";
  url?: string;
  headers?: Record<string, string>;
  expires_at?: string;
}

export interface DownloadLink {
  url: string;
  expires_at: string;
  sha256: string;
  size_bytes: number;
}

// ---- Quote (optional; may be absent or disabled) --------------------------

export interface KitQuote {
  quote_id?: string;
  estimated_minutes: number;
  total?: string;
  currency?: string;
  max_authorized_spend?: string;
  expires_at?: string;
}

export const kitsApi = {
  /** GET /v1/kits — latest published version of each Kit (public). */
  listKits(category?: string): Promise<KitSummary[]> {
    const q = category ? `?category=${encodeURIComponent(category)}` : "";
    return request<KitSummary[]>(`/v1/kits${q}`);
  },
  /** GET /v1/kits/{slug}/{version} — manifest incl. param_schema. */
  getKit(slug: string, version: string): Promise<KitDetail> {
    return request<KitDetail>(`/v1/kits/${encodeURIComponent(slug)}/${encodeURIComponent(version)}`);
  },
  /** GET /v1/kits/{slug} — all versions, newest first. */
  listVersions(slug: string): Promise<KitSummary[]> {
    return request<KitSummary[]>(`/v1/kits/${encodeURIComponent(slug)}`);
  },
  /** POST /v1/kits/validate-spec — 422 carries detail.errors[{field,code,message}]. */
  validateSpec(token: string, spec: JobSpecV2): Promise<ValidateSpecResult> {
    return request<ValidateSpecResult>("/v1/kits/validate-spec", { method: "POST", token, body: spec });
  },
  /** POST /v1/jobs/v2 — idempotent per Idempotency-Key. */
  createJobV2(token: string, input: JobV2CreateInput, idempotencyKey: string): Promise<JobV2> {
    return request<JobV2>("/v1/jobs/v2", {
      method: "POST",
      token,
      body: input,
      headers: { "Idempotency-Key": idempotencyKey },
    });
  },
};

export const dataApi = {
  createUpload(
    token: string,
    body: { name: string; content_type: string; sha256: string; size_bytes: number; retention_days?: number },
    idempotencyKey: string,
  ): Promise<DataUpload> {
    return request<DataUpload>("/v1/data/uploads", {
      method: "POST", token, body, headers: { "Idempotency-Key": idempotencyKey },
    });
  },
  getUpload(token: string, id: string): Promise<UploadState> {
    return request<UploadState>(`/v1/data/uploads/${id}`, { token });
  },
  partUrl(token: string, id: string, part: number, sha256: string, sizeBytes: number): Promise<PartUrl> {
    return request<PartUrl>(`/v1/data/uploads/${id}/parts/${part}/url`, {
      method: "POST", token, body: { sha256, size_bytes: sizeBytes },
    });
  },
  /** 409 {"action":"resend"} means the stored part is corrupt: the caller must re-send it. */
  confirmPart(token: string, id: string, part: number): Promise<{ part_number: number; status: string }> {
    return request(`/v1/data/uploads/${id}/parts/${part}/confirm`, { method: "POST", token, body: {} });
  },
  completeUpload(token: string, id: string): Promise<DataUpload> {
    return request<DataUpload>(`/v1/data/uploads/${id}/complete`, { method: "POST", token, body: {} });
  },
  abortUpload(token: string, id: string): Promise<void> {
    return request<void>(`/v1/data/uploads/${id}`, { method: "DELETE", token });
  },
  /** The caller's files, newest first. `namePrefix` narrows to e.g. a Kit job's outputs (`out/<job_id>/`). */
  listFiles(token: string, opts: { namePrefix?: string; limit?: number } = {}): Promise<DataFile[]> {
    const q = new URLSearchParams();
    if (opts.namePrefix) q.set("name_prefix", opts.namePrefix);
    if (opts.limit) q.set("limit", String(opts.limit));
    const qs = q.toString();
    return request<DataFile[]>(`/v1/data/files${qs ? `?${qs}` : ""}`, { token });
  },
  /** POST /v1/data/files/{id}/download-link — scoped, expiring presigned GET. */
  downloadLink(token: string, fileId: string, ttlSeconds?: number): Promise<DownloadLink> {
    return request<DownloadLink>(`/v1/data/files/${fileId}/download-link`, {
      method: "POST", token, body: ttlSeconds ? { ttl_seconds: ttlSeconds } : {},
    });
  },
};

/** True when a response means the data plane is not switched on (so uploads must be hidden, not failed). */
export function isDataPlaneDisabled(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  const d = (err.details as { detail?: unknown } | undefined)?.detail;
  return err.status === 404 || err.status === 503 || d === "data_plane_disabled";
}

/**
 * Ask for a price/time quote. POST /v1/quotes needs a machine, so a quote exists only when one is chosen.
 * Returns null (never throws) when quotes are absent (404), disabled (501/503), refused (422) or unreachable,
 * so the wizard falls back to the Kit's own estimate.
 */
export async function tryQuote(
  token: string,
  input: { kit: string; expected_units: number; max_cost: string; gpu_count: number },
): Promise<KitQuote | null> {
  try {
    // POST /v1/unit-quotes (U15): 404 while the feature is off, so absence is normal.
    const q = await request<{
      quote_id: string; currency: string; max_cost: string; expires_at: string;
      quote?: { gpu?: { expected?: string } };
    }>("/v1/unit-quotes", { method: "POST", token, body: input });
    return {
      quote_id: q.quote_id,
      estimated_minutes: 0,
      total: q.quote?.gpu?.expected,
      currency: q.currency,
      max_authorized_spend: q.max_cost,
      expires_at: q.expires_at,
    };
  } catch (err) {
    if (err instanceof ApiError) return null;
    throw err;
  }
}

/** Kit-side estimate from the manifest: GPU-seconds per unit times units, spread over the GPUs. */
export function estimateMinutes(manifest: KitManifest, units: number, gpuCount: number): number | null {
  const per = manifest.billing_hints?.est_gpu_seconds_per_unit;
  if (!per || !Number.isFinite(units) || units <= 0) return null;
  return Math.max(1, Math.ceil((per * units) / Math.max(1, gpuCount) / 60));
}

export function formatDuration(minutes: number): string {
  if (minutes < 60) return `about ${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `about ${h} h ${m} min` : `about ${h} h`;
}

/** Messages for Kit submit failures that are not per-field. */
export function kitSubmitErrorMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return "We could not send your job. Check your internet connection and try again.";
  const d = (err.details as { detail?: unknown } | undefined)?.detail;
  const code = typeof d === "string" ? d : "";
  switch (code) {
    case "insufficient_credits": return "You do not have enough credits. Add credits on the Credits and billing page, or lower the most you want to spend.";
    case "spend_limit_exceeded": return "This job would go over a spending limit you set. Spending limits are set through the API (/spend-limits): raise or remove the limit there, or ask for less.";
    case "privacy_tier_not_supported_for_kits": return "Private jobs do not work with Kits yet. Use the Standard privacy level.";
    case "email_not_verified": return "Please confirm your email address before you run jobs. Use the button at the top of the page to get a new link.";
    case "machine_not_found": return "We could not find that computer. Try again and let us pick one for you.";
    default:
      if (extractServerErrors(err).length) return "Some answers need a fix. See the messages below.";
      return err.status === 0 ? err.message : "We could not send your job. Please try again in a moment.";
  }
}

export type { ServerFieldError };
export { apiAbsoluteUrl };

export function verificationLabel(method: string | undefined): string {
  switch (method) {
    case "redundant": return "Re-running a sample on another host";
    case "spot_check": return "Spot checks of sampled results";
    case "none": case undefined: case "": return "Not independently verified";
    default: return method.replace(/_/g, " ");
  }
}
