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
import { GpuHoursChart } from "@/components/console/dashboard-charts";
import { api, ApiError, type Machine, type ProviderEarnings } from "@/lib/api";
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
    { label: "Total machines", value: counts.total, icon: Cpu, tone: "text-primary bg-primary/10" },
    { label: "Online now", value: counts.online, icon: CircleDot, tone: "text-success bg-success/10" },
    { label: "Offline", value: counts.offline, icon: PowerOff, tone: "text-muted-foreground bg-surface-2" },
    { label: "In maintenance", value: counts.maintenance, icon: PauseCircle, tone: "text-warning bg-warning/10" },
  ];

  return (
    <>
      <ConsolePageHeader
        title="Provider overview"
        description="Your machine fleet is live from GET /provider/machines, with live accrued earnings from GET /v1/provider/earnings."
        action={
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
        }
      />

      <div className="space-y-6">
        {/* LIVE fleet status cards. */}
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground">
            Fleet status (live)
          </h2>

          {machinesQuery.isError ? (
            <Card className="border-danger/40">
              <CardContent className="flex items-center gap-3 p-5 text-sm text-danger">
                <AlertCircle className="h-5 w-5 shrink-0" />
                <p>
                  {machinesQuery.error instanceof ApiError
                    ? machinesQuery.error.message
                    : "Couldn't load machine status."}
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
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <Card key={i}>
                  <CardContent className="space-y-2 p-5">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-8 w-12" />
                  </CardContent>
                </Card>
              ))}
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
              Earnings &amp; ledger (live)
            </h2>
            <Link
              href="/provider/earnings"
              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              Detailed breakdown <ArrowRight className="h-3 w-3" />
            </Link>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Card>
              <CardContent className="flex items-start justify-between p-5">
                <div>
                  <p className="text-sm text-muted-foreground">Available for payout</p>
                  <div className="mt-1 font-mono text-2xl font-bold">
                    {earningsQuery.isLoading ? (
                      <Skeleton className="h-8 w-24" />
                    ) : (
                      `${earnings?.available ?? "0.00"} ${earnings?.currency ?? "USDC_CREDITS"}`
                    )}
                  </div>
                  <Badge variant="success" className="mt-2 text-xs">
                    settled
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
                  <p className="text-sm text-muted-foreground">Lifetime earned</p>
                  <div className="mt-1 font-mono text-2xl font-bold">
                    {earningsQuery.isLoading ? (
                      <Skeleton className="h-8 w-24" />
                    ) : (
                      `${earnings?.lifetime ?? "0.00"} ${earnings?.currency ?? "USDC_CREDITS"}`
                    )}
                  </div>
                  <Badge variant="neutral" className="mt-2 text-xs">
                    all-time
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
              <CardTitle>Served GPU hours</CardTitle>
            </CardHeader>
            <CardContent>
              <GpuHoursChart />
            </CardContent>
          </Card>
        </section>
      </div>
    </>
  );
}
