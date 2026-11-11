"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { API_BASE_URL } from "@/lib/api";

interface Health {
  status?: string;
  db?: string;
  redis?: string;
  version?: string;
}

type Tone = "success" | "danger" | "neutral";

/** Live check of the API this site talks to, taken from its own /health endpoint. */
export function LiveStatus() {
  const q = useQuery<Health>({
    queryKey: ["status-health"],
    refetchInterval: 30000,
    retry: false,
    queryFn: async () => {
      const res = await fetch(`${API_BASE_URL}/health`, { cache: "no-store" });
      // 503 is how /health reports "degraded"; its body says which part is down, so read it instead of
      // claiming the whole service is unreachable.
      if (!res.ok && res.status !== 503) throw new Error(`HTTP ${res.status}`);
      return res.json();
    },
  });

  const unreachable = q.isError;
  const h = q.data;
  const row = (name: string, ok: boolean | null): { name: string; state: string; tone: Tone } => ({
    name,
    state: ok === null ? "Checking…" : ok ? "Working" : "Not working",
    tone: ok === null ? "neutral" : ok ? "success" : "danger",
  });
  const loading = q.isLoading;
  const rows = [
    row("ComputeCoin service", loading ? null : !unreachable && h?.status === "ok"),
    row("Data storage", loading ? null : unreachable ? false : h?.db === "up"),
    // "not_configured" means this deployment runs without Redis: that is not a fault.
    row("Speed and safety limits", loading ? null : unreachable ? false : h?.redis !== "down"),
  ];

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <span className="font-medium">Live checks</span>
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">
            {q.dataUpdatedAt ? `Checked ${new Date(q.dataUpdatedAt).toLocaleTimeString()}` : "Checking…"}
            {h?.version ? ` · version ${h.version}` : ""}
          </span>
          <Button variant="outline" size="sm" onClick={() => q.refetch()} disabled={q.isFetching}>
            <RefreshCw className={q.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        </div>
      </div>
      <ul className="divide-y divide-border">
        {rows.map((c) => (
          <li key={c.name} className="flex items-center justify-between px-5 py-4">
            <span className="text-sm">{c.name}</span>
            <Badge variant={c.tone}>{c.state}</Badge>
          </li>
        ))}
      </ul>
      {unreachable ? (
        <p role="alert" className="border-t border-border px-5 py-3 text-sm text-danger">
          We could not reach the ComputeCoin service from your browser. Check your internet connection, then press Refresh. If it keeps failing, the service may be down.
        </p>
      ) : null}
    </div>
  );
}
