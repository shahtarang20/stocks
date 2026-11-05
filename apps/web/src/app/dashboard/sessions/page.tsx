"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Terminal } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, apiErrorCode } from "@/lib/api";
import { sessionsApi, type SessionInfo } from "@/lib/sessions";
import { newClientKey, storeClientKey } from "@/lib/session-crypto";
import { capitalize } from "@/lib/format";
import { HelpTip } from "@/components/ui/help-tip";

function sessionVariant(status: string) {
  if (status === "active") return "success" as const;
  if (status === "failed") return "danger" as const;
  if (status === "pending" || status === "starting") return "default" as const;
  return "neutral" as const;
}

const STOP_REASON_TEXT: Record<string, string> = {
  idle_timeout: "stopped because it sat idle",
  max_duration: "stopped at the time limit",
  user_stopped: "stopped by you",
  host_lost: "the host's computer went offline",
};

const STATUS_TEXT: Record<string, string> = {
  pending: "Waiting for a computer",
  starting: "Starting",
  active: "Running",
  stopped: "Stopped",
  failed: "Failed",
};

function sessionStatusLabel(s: SessionInfo): string {
  const base = STATUS_TEXT[s.status] ?? capitalize(s.status);
  const why = s.stop_reason ? (STOP_REASON_TEXT[s.stop_reason] ?? s.stop_reason.replace(/_/g, " ")) : "";
  return why ? `${base} (${why})` : base;
}

/** Plain words for the error codes the sessions API can return. */
function sessionErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  switch (code) {
    case "insufficient_credits":
      return "You do not have enough credits to start a session. Add credits on the Credits and billing page, then try again.";
    case "kit_not_found":
      return "We could not find that Kit. Check the name on the Kits page and try again.";
    case "kit_mode_not_session":
      return "That Kit cannot be used as a session. Pick a Kit made for sessions, or start it as a normal job from the Kits page.";
    case "email_not_verified":
      return "Please confirm your email address first. Use the button at the top of the page to get a new link.";
    default:
      if (err instanceof ApiError && err.message && !/^[a-z0-9]+(_[a-z0-9]+)+$/.test(err.message)) return err.message;
      return "We could not start the session. Check the details and try again in a moment.";
  }
}

export default function SessionsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [kit, setKit] = useState("");
  // The Kits page links here as /dashboard/sessions?kit=<slug>@<version>.
  useEffect(() => {
    const k = new URLSearchParams(window.location.search).get("kit");
    if (k) setKit(k.slice(0, 200));
  }, []);
  const [price, setPrice] = useState("1.00");
  const [maxMin, setMaxMin] = useState("60");
  const [idleMin, setIdleMin] = useState("15");
  const list = useQuery({ queryKey: ["sessions"], queryFn: sessionsApi.list, refetchInterval: 5000 });
  const create = useMutation({
    mutationFn: async () => {
      const key = newClientKey();
      const s = await sessionsApi.create({
        kit_ref: kit.trim(), gpu_count: 1, max_price_per_gpu_hour: price,
        max_duration_seconds: Math.round(Number(maxMin) * 60), idle_timeout_seconds: Math.round(Number(idleMin) * 60),
        client_pubkey: key.pubHex,
      });
      storeClientKey(s.id, key.priv);
      return s;
    },
    onSuccess: (s) => { qc.invalidateQueries({ queryKey: ["sessions"] }); router.push(`/dashboard/sessions/${s.id}`); },
  });
  const err = create.error ? sessionErrorMessage(create.error) : null;
  const sessions = list.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Sessions"
        description="A session gives you a command window (a terminal) on a host's computer with a graphics card. You only pay while it is running, and it stops by itself if you leave it idle. What you type is locked between your browser and the host's computer, so the service in the middle cannot read it."
      />
      <Card className="mb-6">
        <CardContent className="p-6">
          <h2 className="mb-4 text-sm font-semibold">
            Start a session
            <HelpTip term="locked end to end">
              Your browser and the host&apos;s computer share a secret key that only they know. The service
              that passes messages between them sees only scrambled data.
            </HelpTip>
          </h2>
          <form
            className="grid gap-4 sm:grid-cols-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (kit.trim() && !create.isPending) create.mutate();
            }}
          >
            <div className="space-y-1.5 sm:col-span-4">
              <Label htmlFor="kit">Which Kit to run</Label>
              <Input id="kit" value={kit} onChange={(e) => setKit(e.target.value)} placeholder="terminal-gpu@1.0.0" autoComplete="off" required aria-describedby="kit-d" />
              <p id="kit-d" className="text-xs text-muted-foreground">Type the Kit name, then @ and its version, like terminal-gpu@1.0.0. You can find Kit names on the Kits page.</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="p">Most you will pay per hour (credits)</Label>
              <Input id="p" inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="m">Longest it can run (minutes)</Label>
              <Input id="m" inputMode="numeric" value={maxMin} onChange={(e) => setMaxMin(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="i">Stop if idle for (minutes)</Label>
              <Input id="i" inputMode="numeric" value={idleMin} onChange={(e) => setIdleMin(e.target.value)} />
            </div>
            <div className="flex items-end">
              <Button type="submit" className="w-full sm:w-auto" disabled={!kit.trim() || create.isPending}>
                {create.isPending ? <Loader2 className="animate-spin" /> : null}
                {create.isPending ? "Starting…" : "Start session"}
              </Button>
            </div>
            {err ? <p role="alert" className="text-sm text-danger sm:col-span-4">{err}</p> : null}
          </form>
        </CardContent>
      </Card>

      <h2 className="mb-3 text-sm font-semibold">Your sessions</h2>
      {list.isLoading ? (
        <TableSkeleton rows={3} label="Loading sessions" />
      ) : list.isError ? (
        <ErrorState
          title="We could not load your sessions"
          message={list.error instanceof ApiError ? list.error.message : undefined}
          onRetry={() => list.refetch()}
          retrying={list.isFetching}
        />
      ) : sessions.length === 0 ? (
        <EmptyState icon={Terminal} title="You have no sessions yet">
          Type a Kit name above and press Start session. Your command window opens on a host&apos;s computer.
        </EmptyState>
      ) : (
        <ul className="space-y-2">
          {sessions.map((s) => (
            <li key={s.id}>
              <Card>
                <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
                  <div className="min-w-0">
                    <Link className="break-all font-medium text-primary hover:underline" href={`/dashboard/sessions/${s.id}`}>
                      {s.kit_ref ?? s.id}
                    </Link>
                    <p className="text-xs text-muted-foreground">
                      {s.gpu_count} graphics card, {s.held} credits set aside
                      {s.captured ? `, charged ${s.captured}` : ""} · started <RelativeTime iso={s.created_at} />
                    </p>
                  </div>
                  <Badge variant={sessionVariant(s.status)}>{sessionStatusLabel(s)}</Badge>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
