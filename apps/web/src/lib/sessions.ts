/** Typed client for interactive sessions (U10). */
import { API_BASE_URL, request } from "@/lib/api";

/** One file the customer asked to have placed in the workspace (path and size only; never a download link). */
export interface SessionInput {
  file_id?: string;
  path: string;
  size_bytes: number;
  sha256?: string;
}

/** One result file the host saved to the customer's storage. */
export interface SessionExport {
  name: string;
  size_bytes: number;
  file_id: string;
  sha256?: string;
}

/** "requested" (waiting for the host) | "running" | "done" | "failed" | null (nothing asked for yet). */
export type ExportState = "requested" | "running" | "done" | "failed" | null;

export interface SessionInfo {
  id: string;
  status: "pending" | "starting" | "active" | "stopped" | "failed" | string;
  stop_reason: string | null;
  kit_ref: string | null;
  mode: "pty" | "tcp" | string;
  gpu_count: number;
  max_duration_seconds: number;
  idle_timeout_seconds: number;
  held: string;
  captured: string | null;
  active_seconds: number | null;
  host_pubkey: string | null;
  created_at: string;
  // Container rental (all absent or null for a Kit session and for older API versions).
  rental?: boolean;
  template?: string | null;
  workspace_gb?: number;
  memory_mb?: number | null;
  cpus?: number | null;
  machine_id?: string | null;
  requested_machine_id?: string | null;
  inputs?: SessionInput[] | null;
  export_state?: ExportState;
  exports?: SessionExport[] | null;
}

export interface SessionCreateInput {
  /** Exactly one of kit_ref, image_digest or template. */
  kit_ref?: string;
  image_digest?: string;
  template?: string;
  gpu_count: number;
  max_price_per_gpu_hour: string;
  max_duration_seconds: number;
  idle_timeout_seconds: number;
  client_pubkey: string;
  // Container rental only.
  machine_id?: string;
  workspace_gb?: number;
  memory_mb?: number;
  cpus?: number;
  command?: string[];
  mode?: "pty" | "tcp";
  tcp_port?: number;
  network_policy?: { egress: string[] };
  inputs?: { file_id: string; path: string }[];
  accept_rental_terms?: boolean;
  isolation_tier?: "container";
}

/** A ready-made rental template (GET /v1/rental/templates). The API never sends its image digest. */
export interface RentalTemplate {
  slug: string;
  name: string;
  description: string;
  gpu_required: boolean;
  mode: "pty" | "tcp" | string;
  default_egress: string[];
  libraries: string[];
}

export const sessionsApi = {
  create: (body: SessionCreateInput) => request<SessionInfo>("/v1/sessions", { method: "POST", body }),
  list: () => request<SessionInfo[]>("/v1/sessions"),
  get: (id: string) => request<SessionInfo>(`/v1/sessions/${id}`),
  stop: (id: string) => request<SessionInfo>(`/v1/sessions/${id}/stop`, { method: "POST", body: {} }),
  /** GET /v1/rental/templates — public; only templates that are built and ready. */
  listTemplates: () => request<RentalTemplate[]>("/v1/rental/templates"),
  /** POST /v1/sessions/{id}/export — ask the host to save /workspace/results to the customer's storage now. */
  requestExport: (id: string) =>
    request<{ export_state: ExportState }>(`/v1/sessions/${id}/export`, { method: "POST", body: {} }),
  connectToken: (id: string) =>
    request<{ token: string; expires_in: number; host_pubkey: string | null; status: string }>(
      `/v1/sessions/${id}/connect-token`, { method: "POST", body: {} },
    ),
  relayUrl(id: string, token: string): string {
    const base = API_BASE_URL.replace(/^http/, "ws");
    return `${base}/v1/sessions/${id}/relay/browser?token=${encodeURIComponent(token)}`;
  },
};
