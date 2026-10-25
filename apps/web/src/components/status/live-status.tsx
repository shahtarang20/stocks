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
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    },
  });

  const unreachable = q.isError;
  const h = q.data;
  const row = (name: string, ok: boolean | null): { name: string; state: string; tone: Tone } => ({
    name,
    state: ok === null ? "Checking…" : ok ? "Operational" : "Unavailable",
    tone: ok === null ? "neutral" : ok ? "success" : "danger",
  });
  const loading = q.isLoading;
  const rows = [
    row("API", loading ? null : !unreachable && h?.status === "ok"),
    row("Database", loading ? null : unreachable ? false : h?.db === "up"),
    row("Cache and rate limiting", loading ? null : unreachable ? false : h?.redis === "up"),
  ];

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <span className="font-medium">Live checks</span>
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">
            {q.dataUpdatedAt ? `Checked ${new Date(q.dataUpdatedAt).toLocaleTimeString()}` : "Checking…"}
            {h?.version ? ` · API v${h.version}` : ""}
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
          The API could not be reached from your browser just now.
        </p>
      ) : null}
    </div>
  );
}
