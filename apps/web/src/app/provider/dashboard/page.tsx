"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  CircleDot,
  Cpu,
  PauseCircle,
  PowerOff,
  RefreshCw,
  Wallet,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { DemoBanner } from "@/components/demo-banner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { GpuHoursChart } from "@/components/console/dashboard-charts";
import { api, ApiError, type Machine } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatUsd } from "@/lib/utils";

const DEMO_STATS = [
  { label: "Utilization (7d)", value: "71%", icon: Activity },
  { label: "Earnings (7d)", value: formatUsd(512, 0), icon: Wallet },
];

export default function ProviderDashboardPage() {
  const { accessToken } = useAuth();

  const machinesQuery = useQuery<Machine[]>({
    queryKey: ["provider", "machines"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listMachines(accessToken as string),
  });

  const machines = machinesQuery.data ?? [];
  const counts = {
    total: machines.length,
    online: machines.filter((m) => m.status === "online").length,
    offline: machines.filter((m) => m.status === "offline").length,
    maintenance: machines.filter((m) => m.status === "maintenance").length,
  };

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
        description="Your machine fleet is live from GET /provider/machines. Earnings and utilization remain demo until billing is wired."
        action={
          <Button
            variant="outline"
            onClick={() => machinesQuery.refetch()}
            disabled={machinesQuery.isFetching}
          >
            <RefreshCw
              className={machinesQuery.isFetching ? "animate-spin" : undefined}
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
              <CardContent className="flex items-center gap-3 p-5 text-sm">
                <AlertCircle className="h-5 w-5 shrink-0 text-danger" />
                <span className="text-muted-foreground">
                  {machinesQuery.error instanceof ApiError
                    ? machinesQuery.error.message
                    : "Couldn't load your machines."}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  className="ml-auto"
                  onClick={() => machinesQuery.refetch()}
                >
                  <RefreshCw /> Retry
                </Button>
              </CardContent>
            </Card>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {liveCards.map((c) => (
                <Card key={c.label}>
                  <CardContent className="flex items-start justify-between p-5">
                    <div>
                      <p className="text-sm text-muted-foreground">{c.label}</p>
                      {machinesQuery.isLoading ? (
                        <Skeleton className="mt-2 h-8 w-10" />
                      ) : (
                        <p className="mt-1 text-2xl font-bold">{c.value}</p>
                      )}
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

        {/* DEMO earnings / utilization. */}
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground">
            Earnings &amp; utilization
          </h2>
          <DemoBanner>
            earnings and utilization are placeholder figures. Billing and
            metering are not yet wired, so these do not reflect real payouts.
          </DemoBanner>

          <div className="grid gap-4 sm:grid-cols-2">
            {DEMO_STATS.map((s) => (
              <Card key={s.label}>
                <CardContent className="flex items-start justify-between p-5">
                  <div>
                    <p className="text-sm text-muted-foreground">{s.label}</p>
                    <p className="mt-1 text-2xl font-bold">{s.value}</p>
                    <Badge variant="neutral" className="mt-2">
                      demo
                    </Badge>
                  </div>
                  <span className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
                    <s.icon className="h-4 w-4" />
                  </span>
                </CardContent>
              </Card>
            ))}
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Served GPU hours (demo)</CardTitle>
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
