"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  CircleDot,
  Cpu,
  PauseCircle,
  PowerOff,
  RefreshCw,
  Wallet,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RelativeTime } from "@/components/console/bits";
import { api, ApiError, type Machine, type ProviderEarnings } from "@/lib/api";
import { creditsName } from "@/lib/format";
import { useAuth } from "@/lib/auth-context";

export default function ProviderDashboardPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const machinesQuery = useQuery<Machine[]>({
    queryKey: ["provider", "machines"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listMachines(token),
  });

  const earningsQuery = useQuery<ProviderEarnings>({
    queryKey: ["provider-earnings"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getProviderEarnings(token),
  });

  const machines = machinesQuery.data ?? [];
  const earnings = earningsQuery.data;

  const counts = {
    total: machines.length,
    online: machines.filter((m) => m.status === "online").length,
    offline: machines.filter((m) => m.status === "offline").length,
    maintenance: machines.filter((m) => m.status === "maintenance").length,
  };

  const isRefreshing = machinesQuery.isFetching || earningsQuery.isFetching;

  function handleRefresh() {
    machinesQuery.refetch();
    earningsQuery.refetch();
  }

  const liveCards = [
    { label: "Your computers", value: counts.total, icon: Cpu, tone: "text-primary bg-primary/10" },
    { label: "Online now", value: counts.online, icon: CircleDot, tone: "text-success bg-success/10" },
    { label: "Offline", value: counts.offline, icon: PowerOff, tone: "text-muted-foreground bg-surface-2" },
    { label: "Paused (maintenance)", value: counts.maintenance, icon: PauseCircle, tone: "text-warning bg-warning/10" },
  ];

  return (
    <>
      <ConsolePageHeader
        title="Host overview"
        description="See which of your computers are online and what they have earned so far. This page updates by itself. Earnings are test credits, not real money."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={handleRefresh}
              disabled={isRefreshing}
            >
              <RefreshCw
                className={isRefreshing ? "animate-spin" : undefined}
              />
              Refresh
            </Button>
            <Button asChild>
              <Link href="/provider/onboarding">Add my computer</Link>
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        {/* LIVE fleet status cards. */}
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground">
            Your computers
          </h2>

          {machinesQuery.isError ? (
            <Card className="border-danger/40">
              <CardContent role="alert" className="flex flex-wrap items-center gap-3 p-5 text-sm text-danger">
                <AlertCircle className="h-5 w-5 shrink-0" />
                <p>
                  {machinesQuery.error instanceof ApiError
                    ? machinesQuery.error.message
                    : "We could not load your computers. Press Try again."}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => machinesQuery.refetch()}
                  className="ml-auto"
                >
                  Try again
                </Button>
              </CardContent>
            </Card>
          ) : machinesQuery.isLoading ? (
            <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <Card key={i}>
                  <CardContent className="space-y-2 p-5">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-8 w-12" />
                  </CardContent>
                </Card>
              ))}
            </div>
          ) : counts.total === 0 ? (
            <Card className="border-dashed">
              <CardContent className="flex flex-col items-center gap-3 p-8 text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
                  <Cpu className="h-6 w-6" />
                </span>
                <h3 className="font-semibold">You have not added a computer yet</h3>
                <p className="max-w-md text-sm text-muted-foreground">
                  Add your computer to share its graphics card (GPU) on ComputeCoin. The setup guide
                  takes you through it step by step.
                </p>
                <Button asChild>
                  <Link href="/provider/onboarding">Start the setup guide</Link>
                </Button>
              </CardContent>
            </Card>
          ) : (
            <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
              {liveCards.map((c) => (
                <Card key={c.label}>
                  <CardContent className="flex items-start justify-between p-5">
                    <div>
                      <p className="text-sm text-muted-foreground">{c.label}</p>
                      <p className="mt-1 font-mono text-2xl font-bold">
                        {c.value}
                      </p>
                    </div>
                    <span
                      className={`flex h-9 w-9 items-center justify-center rounded-md ${c.tone}`}
                    >
                      <c.icon className="h-4 w-4" />
                    </span>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </section>

        {/* LIVE earnings from ledger. */}
        <section className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-muted-foreground">
              Earnings
            </h2>
            <Link
              href="/provider/earnings"
              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              See all earnings <ArrowRight className="h-3 w-3" />
            </Link>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Card>
              <CardContent className="flex items-start justify-between p-5">
                <div>
                  <p className="text-sm text-muted-foreground">Ready to pay out</p>
                  <div className="mt-1 font-mono text-2xl font-bold">
                    {earningsQuery.isLoading ? (
                      <Skeleton className="h-8 w-24" />
                    ) : (
                      <>
                        {earnings?.available ?? "0.00"}{" "}
                        <span className="text-xs font-medium text-muted-foreground">{creditsName(earnings?.currency)}</span>
                      </>
                    )}
                  </div>
                  <Badge variant="success" className="mt-2 text-xs">
                    Earned
                  </Badge>
                </div>
                <span className="flex h-9 w-9 items-center justify-center rounded-md bg-success/10 text-success">
                  <Wallet className="h-4 w-4" />
                </span>
              </CardContent>
            </Card>

            <Card>
              <CardContent className="flex items-start justify-between p-5">
                <div>
                  <p className="text-sm text-muted-foreground">Total earned so far</p>
                  <div className="mt-1 font-mono text-2xl font-bold">
                    {earningsQuery.isLoading ? (
                      <Skeleton className="h-8 w-24" />
                    ) : (
                      <>
                        {earnings?.lifetime ?? "0.00"}{" "}
                        <span className="text-xs font-medium text-muted-foreground">{creditsName(earnings?.currency)}</span>
                      </>
                    )}
                  </div>
                  <Badge variant="neutral" className="mt-2 text-xs">
                    Since you joined
                  </Badge>
                </div>
                <span className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <Activity className="h-4 w-4" />
                </span>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Recent earnings</CardTitle>
            </CardHeader>
            <CardContent>
              {earningsQuery.isLoading ? (
                <Skeleton className="h-20 w-full" />
              ) : (earnings?.recent ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No earnings yet. When a customer job finishes on your computer, your share shows up here.
                </p>
              ) : (
                <ul className="divide-y divide-border text-sm">
                  {(earnings?.recent ?? []).slice(0, 8).map((e, i) => (
                    <li key={i} className="flex items-center justify-between gap-3 py-2">
                      <span className="text-muted-foreground">
                        {String(e.kind ?? "earning").replace(/_/g, " ")}
                        {e.created_at ? <> · <RelativeTime iso={String(e.created_at)} /></> : null}
                      </span>
                      <span className="font-mono tabular-nums">
                        {e.direction === "debit" ? "−" : "+"}
                        {String(e.amount)} {creditsName(earnings?.currency)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </section>
      </div>
    </>
  );
}
