"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { API_BASE_URL, getStoredAccessToken, request } from "@/lib/api";

/** GET /v1/jobs/{id}/live (JobLiveOut). All metrics are optional: the agent may not have reported yet. */
export interface JobLive {
  job_id: string;
  status: string;
  sampled_at?: string | null;
  gpu_util_pct?: number | null;
  mem_used_mb?: number | null;
  mem_total_mb?: number | null;
  progress_pct?: number | null;
  phase?: string | null;
  elapsed_seconds?: number | null;
  cost_cap?: number | null;
  cost_so_far?: number | null;
  cost_so_far_is_estimate?: boolean;
}

const POLL_MS = 5000;
const TERMINAL = new Set(["completed", "failed", "timed_out", "cancelled", "disputed"]);

/** Parse a server-sent-events chunk buffer into events; returns the unfinished tail. */
export function parseSse(buffer: string): { events: { event: string; data: string }[]; rest: string } {
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  const events = parts
    .map((block) => {
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      return { event, data: data.join("\n") };
    })
    .filter((e) => e.data !== "" || e.event !== "message");
  return { events, rest };
}

function pct(v: number | null | undefined): string {
  return v == null ? "n/a" : `${Math.max(0, Math.min(100, v)).toFixed(0)}%`;
}

/**
 * Live data for a RUNNING job: polls GET /v1/jobs/{id}/live and, as the fast path, reads the
 * /live/stream event stream with fetch (EventSource cannot send the bearer token). If the stream
 * fails the polling keeps the panel current. The cost is an estimate from the cap and elapsed time,
 * never the billed amount.
 */
export function LivePanel({ jobId, running }: { jobId: string; running: boolean }) {
  const [live, setLive] = useState<JobLive | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const abort = new AbortController();

    async function poll() {
      try {
        const data = await request<JobLive>(`/v1/jobs/${encodeURIComponent(jobId)}/live`, { method: "GET" });
        if (!cancelled) {
          setLive(data);
          setError(null);
        }
      } catch {
        if (!cancelled) setError("Live numbers are not available right now. The job is still running. Check back in a moment.");
      }
    }

    async function stream() {
      while (!cancelled) {
        try {
          const token = getStoredAccessToken();
          const res = await fetch(`${API_BASE_URL}/v1/jobs/${encodeURIComponent(jobId)}/live/stream`, {
            method: "GET",
            credentials: "include",
            headers: { Accept: "text/event-stream", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
            signal: abort.signal,
          });
          if (!res.ok || !res.body) return; // polling carries on
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const parsed = parseSse(buf);
            buf = parsed.rest;
            for (const ev of parsed.events) {
              if (ev.event === "end") return;
              if (ev.event === "reconnect") continue;
              try {
                const data = JSON.parse(ev.data) as JobLive;
                if (!cancelled) setLive(data);
                if (TERMINAL.has(data.status)) return;
              } catch {
                /* ignore a malformed event */
              }
            }
          }
        } catch {
          if (cancelled || abort.signal.aborted) return;
          return; // network error: fall back to polling only
        }
        // The server closes the stream after its maximum time and sends "reconnect": loop and reconnect.
        // A proxy that drops the stream without any event must not turn this into a hot reconnect loop.
        await new Promise((r) => setTimeout(r, 1000));
      }
    }

    void poll();
    const timer = setInterval(poll, POLL_MS);
    void stream();
    return () => {
      cancelled = true;
      abort.abort();
      clearInterval(timer);
    };
  }, [jobId, running]);

  if (!running) return null;

  const memPct =
    live?.mem_used_mb != null && live?.mem_total_mb ? (live.mem_used_mb / live.mem_total_mb) * 100 : null;
  const progress = live?.progress_pct ?? null;

  return (
    <Card>
      <CardContent className="p-6">
        <h2 className="mb-3 text-sm font-semibold">Live view of the computer</h2>
        {error && !live ? <p className="text-xs text-muted-foreground">{error}</p> : null}
        {!live && !error ? <p className="text-xs text-muted-foreground">Waiting for the first numbers from the computer…</p> : null}
        {live ? (
          <dl className="grid gap-3 text-sm sm:grid-cols-2" aria-live="polite">
            <div>
              <dt className="text-muted-foreground">Graphics card (GPU) busy</dt>
              <dd className="mt-0.5 font-mono tabular-nums">{pct(live.gpu_util_pct)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Graphics card memory used</dt>
              <dd className="mt-0.5 font-mono tabular-nums">
                {live.mem_used_mb != null
                  ? `${live.mem_used_mb} MB${live.mem_total_mb ? ` of ${live.mem_total_mb} MB (${pct(memPct)})` : ""}`
                  : "n/a"}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-muted-foreground">
                Progress{live.phase ? <span className="ml-2 text-xs">({live.phase})</span> : null}
              </dt>
              <dd className="mt-1">
                <div
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={progress != null ? Math.round(progress) : undefined}
                  className="h-2 w-full overflow-hidden rounded bg-muted"
                >
                  <div className="h-full bg-primary" style={{ width: `${progress != null ? Math.max(0, Math.min(100, progress)) : 0}%` }} />
                </div>
                <span className="mt-1 block font-mono text-xs tabular-nums">{pct(progress)}</span>
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-muted-foreground">Cost so far (a guess)</dt>
              <dd className="mt-0.5">
                <span className="font-mono tabular-nums">
                  {live.cost_so_far != null ? `~${live.cost_so_far}` : "n/a"}
                </span>
                {live.cost_cap != null ? (
                  <span className="ml-2 text-xs text-muted-foreground">of the most you agreed to pay ({live.cost_cap})</span>
                ) : null}
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  This is a rough guess from the time used so far. It is not the final bill. The final cost shows when the job is done.
                </span>
              </dd>
            </div>
          </dl>
        ) : null}
      </CardContent>
    </Card>
  );
}
