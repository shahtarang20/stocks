/** Typed client for interactive sessions (U10). */
import { API_BASE_URL, request } from "@/lib/api";

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
}

export interface SessionCreateInput {
  kit_ref: string;
  gpu_count: number;
  max_price_per_gpu_hour: string;
  max_duration_seconds: number;
  idle_timeout_seconds: number;
  client_pubkey: string;
}

export const sessionsApi = {
  create: (body: SessionCreateInput) => request<SessionInfo>("/v1/sessions", { method: "POST", body }),
  list: () => request<SessionInfo[]>("/v1/sessions"),
  get: (id: string) => request<SessionInfo>(`/v1/sessions/${id}`),
  stop: (id: string) => request<SessionInfo>(`/v1/sessions/${id}/stop`, { method: "POST", body: {} }),
  connectToken: (id: string) =>
    request<{ token: string; expires_in: number; host_pubkey: string | null; status: string }>(
      `/v1/sessions/${id}/connect-token`, { method: "POST", body: {} },
    ),
  relayUrl(id: string, token: string): string {
    const base = API_BASE_URL.replace(/^http/, "ws");
    return `${base}/v1/sessions/${id}/relay/browser?token=${encodeURIComponent(token)}`;
  },
};
