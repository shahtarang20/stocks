"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Flame,
  Info,
  Power,
  RefreshCw,
  ShieldAlert,
  Thermometer,
  Zap,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HelpTip } from "@/components/ui/help-tip";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError, type TelemetryAlert } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";
import {
  capabilityLabels,
  filterByPlatform,
  platformCounts,
  platformLabel,
  platformOf,
  type PlatformFilter,
} from "@/lib/fleet-platform";

function severityBadge(sev: string) {
  if (sev === "critical") {
    return <Badge variant="danger" className="uppercase font-semibold">Critical</Badge>;
  }
  if (sev === "warning") {
    return <Badge variant="warning" className="uppercase font-semibold">Warning</Badge>;
  }
  return <Badge variant="neutral" className="uppercase font-semibold">Info</Badge>;
}

export default function AdminTelemetryPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const qc = useQueryClient();
  const [selectedMachineId, setSelectedMachineId] = useState<string>("");
  const [platformFilter, setPlatformFilter] = useState<PlatformFilter>("all");

  // 1. Platform-wide telemetry stats
  const statsQuery = useQuery({
    queryKey: ["admin", "telemetry-stats"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getPlatformTelemetryStats(token),
    refetchInterval: 10000,
  });

  // 2. Open alerts query
  const alertsQuery = useQuery({
    queryKey: ["admin", "telemetry-alerts"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listTelemetryAlerts(token),
    refetchInterval: 10000,
  });

  // 3. Provider machines list for dropdown
  const machinesQuery = useQuery({
    queryKey: ["admin", "machines-list"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listAdminMachines(token),
    refetchInterval: 30000,
  });

  // 4. Selected machine telemetry drilldown
  const machineTelemetryQuery = useQuery({
    queryKey: ["admin", "machine-telemetry", selectedMachineId],
    enabled: Boolean(accessToken && selectedMachineId),
    queryFn: () => api.getMachineTelemetry(token, selectedMachineId, 60),
    refetchInterval: 10000,
  });

  // Mutations for alerts
  const ackMut = useMutation({
    mutationFn: (alertId: string) => api.acknowledgeAlert(token, alertId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "telemetry-alerts"] });
      qc.invalidateQueries({ queryKey: ["admin", "telemetry-stats"] });
    },
  });

  const resolveMut = useMutation({
    mutationFn: (alertId: string) => api.resolveAlert(token, alertId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "telemetry-alerts"] });
      qc.invalidateQueries({ queryKey: ["admin", "telemetry-stats"] });
    },
  });

  const stats = statsQuery.data;
  const alerts = alertsQuery.data ?? [];
  const machines = machinesQuery.data ?? [];
  const machineTelemetry = machineTelemetryQuery.data;

  // Auto-select first machine if none selected
  if (!selectedMachineId && machines.length > 0) {
    setSelectedMachineId(machines[0].machine_id);
  }

  const isFetchingAny =
    statsQuery.isFetching || alertsQuery.isFetching || machineTelemetryQuery.isFetching;

  return (
    <>
      <ConsolePageHeader
        title="Computer health"
        description="Live temperature, memory and power readings from host computers. Look for open alerts below, pick a computer to see its details, and mark each alert as seen or fixed."
        action={
          <Button
            variant="outline"
            onClick={() => {
              statsQuery.refetch();
              alertsQuery.refetch();
              machineTelemetryQuery.refetch();
            }}
            disabled={isFetchingAny}
          >
            <RefreshCw className={isFetchingAny ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {/* KPI Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* Active GPUs */}
        <Card className="relative overflow-hidden border-border/60 bg-gradient-to-br from-card to-card/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Graphics cards reporting
            </CardTitle>
            <Cpu className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            {statsQuery.isLoading ? (
              <Skeleton className="h-8 w-24" />
            ) : (
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-bold tracking-tight">
                  {stats?.active_gpus ?? 0}
                </span>
                <span className="text-xs text-muted-foreground">online now</span>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Fleet GPU Utilization */}
        <Card className="relative overflow-hidden border-border/60 bg-gradient-to-br from-card to-card/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Average GPU use
            </CardTitle>
            <Activity className="h-4 w-4 text-info" />
          </CardHeader>
          <CardContent>
            {statsQuery.isLoading ? (
              <Skeleton className="h-8 w-24" />
            ) : (
              <div>
                <div className="text-2xl font-bold tracking-tight">
                  {stats?.fleet_avg_gpu_util_pct ?? 0}%
                </div>
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-secondary">
                  <div
                    className="h-full bg-primary transition-all duration-500"
                    style={{ width: `${Math.min(100, stats?.fleet_avg_gpu_util_pct ?? 0)}%` }}
                  />
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Fleet Temperature & Power */}
        <Card className="relative overflow-hidden border-border/60 bg-gradient-to-br from-card to-card/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Average heat and power
            </CardTitle>
            <Thermometer className="h-4 w-4 text-warning" />
          </CardHeader>
          <CardContent>
            {statsQuery.isLoading ? (
              <Skeleton className="h-8 w-24" />
            ) : (
              <div className="flex items-baseline justify-between">
                <div>
                  <span className="text-2xl font-bold tracking-tight">
                    {stats?.fleet_avg_temp_c ?? 0}°C
                  </span>
                  <span className="ml-1 text-xs text-muted-foreground">average heat</span>
                </div>
                <div className="text-right">
                  <div className="text-sm font-semibold text-foreground">
                    {stats?.fleet_total_power_kw ?? 0} kW
                  </div>
                  <div className="text-xs text-muted-foreground">total power use</div>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Active Alerts */}
        <Card className="relative overflow-hidden border-border/60 bg-gradient-to-br from-card to-card/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Open alerts
            </CardTitle>
            <ShieldAlert className="h-4 w-4 text-danger" />
          </CardHeader>
          <CardContent>
            {statsQuery.isLoading ? (
              <Skeleton className="h-8 w-24" />
            ) : (
              <div className="flex items-baseline gap-2">
                <span
                  className={`text-2xl font-bold tracking-tight ${
                    (stats?.active_alerts_count ?? 0) > 0 ? "text-danger" : "text-success"
                  }`}
                >
                  {stats?.active_alerts_count ?? 0}
                </span>
                <span className="text-xs text-muted-foreground">
                  ({stats?.critical_alerts_count ?? 0} critical)
                </span>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* B6: machines whose agent reports a thermal throttle (no new jobs are scheduled to them) */}
      {machines.some((m) => m.availability === "throttled") ? (
        <Card className="mt-6 border-warning/50">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base font-semibold">
              <Flame className="h-4 w-4 text-warning" />
              Computers that are too hot
              <Badge variant="warning">{machines.filter((m) => m.availability === "throttled").length}</Badge>
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              These computers paused because their graphics card got too hot. No new jobs go to them until they cool down and report online again.
            </p>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {machines
              .filter((m) => m.availability === "throttled")
              .map((m) => (
                <button
                  key={m.machine_id}
                  type="button"
                  onClick={() => setSelectedMachineId(m.machine_id)}
                  className="rounded-md border border-border/60 px-2 py-1 font-mono text-xs hover:bg-muted"
                >
                  {m.hostname} ({m.machine_id.slice(0, 8)}…)
                </button>
              ))}
          </CardContent>
        </Card>
      ) : null}

      {/* Operational Alerts Management Table */}
      <Card className="mt-6">
        <CardHeader className="border-b border-border/40 pb-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="text-base font-semibold">Open alerts</CardTitle>
              <p className="text-xs text-muted-foreground">
                Alerts appear when a graphics card goes above 85°C or its memory is more than 95% full. Mark an alert as seen when you start looking, and as fixed when it is solved.
              </p>
            </div>
            <Badge
              className="shrink-0 whitespace-nowrap"
              variant={alerts.filter((a) => a.status === "active").length > 0 ? "danger" : "outline"}
            >
              {alerts.filter((a) => a.status === "active").length} open
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {alertsQuery.isLoading ? (
            <div className="p-6 space-y-3">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </div>
          ) : alerts.length === 0 ? (
            <div className="p-10 text-center text-sm text-muted-foreground">
              <CheckCircle2 className="mx-auto mb-2 h-6 w-6 text-success" />
              No alerts. All computers are running within normal heat and memory limits. Nothing to do.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Severity</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Message</TableHead>
                  <TableHead>
                    Reading / limit
                    <HelpTip term="reading and limit">The first number is what the computer reported. The second is the level that sets off the alert.</HelpTip>
                  </TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Reported</TableHead>
                  <TableHead className="text-right">What to do</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {alerts.map((alert: TelemetryAlert) => (
                  <TableRow key={alert.id}>
                    <TableCell>{severityBadge(alert.severity)}</TableCell>
                    <TableCell className="font-mono text-xs">{alert.alert_type}</TableCell>
                    <TableCell className="text-sm font-medium">{alert.message}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {alert.metric_value != null ? alert.metric_value.toFixed(1) : "—"} /{" "}
                      {alert.threshold != null ? `${alert.threshold.toFixed(1)}` : "—"}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          alert.status === "active"
                            ? "danger"
                            : alert.status === "acknowledged"
                            ? "warning"
                            : "outline"
                        }
                      >
                        {alert.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatRelativeTime(alert.created_at)}
                    </TableCell>
                    <TableCell className="text-right space-x-2">
                      {alert.status === "active" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => ackMut.mutate(alert.id)}
                          disabled={ackMut.isPending}
                        >
                          Mark as seen
                        </Button>
                      ) : null}
                      {alert.status !== "resolved" ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => resolveMut.mutate(alert.id)}
                          disabled={resolveMut.isPending}
                        >
                          Mark as fixed
                        </Button>
                      ) : (
                        <span className="text-xs text-muted-foreground">Fixed</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* W1 / W10: fleet view with each computer's operating system and the abilities its agent reports */}
      <Card className="mt-6">
        <CardHeader className="border-b border-border/40 pb-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle className="text-base font-semibold">Fleet: system and abilities</CardTitle>
              <p className="text-xs text-muted-foreground">
                What each computer says it runs on and what it can do. Jobs are only sent to computers that report the ability they need. Windows computers run standard container jobs only.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <label htmlFor="admin-fleet-platform" className="text-xs text-muted-foreground">System:</label>
              <select
                id="admin-fleet-platform"
                value={platformFilter}
                onChange={(e) => setPlatformFilter(e.target.value as PlatformFilter)}
                className="rounded-md border border-input bg-background px-3 py-1.5 text-xs text-foreground shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
              >
                <option value="all">All ({platformCounts(machines).all})</option>
                <option value="linux">Linux ({platformCounts(machines).linux})</option>
                <option value="windows">Windows ({platformCounts(machines).windows})</option>
                <option value="unreported">Not reported ({platformCounts(machines).unreported})</option>
              </select>
            </div>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {filterByPlatform(machines, platformFilter).length === 0 ? (
            <div className="p-8 text-center text-sm text-muted-foreground">
              No computers match this filter.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Computer</TableHead>
                  <TableHead>System</TableHead>
                  <TableHead>Agent</TableHead>
                  <TableHead>Abilities</TableHead>
                  <TableHead>State</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filterByPlatform(machines, platformFilter).map((m) => (
                  <TableRow key={m.machine_id} data-platform={platformOf(m)}>
                    <TableCell className="font-mono text-xs">
                      {m.hostname} ({m.machine_id.slice(0, 8)}…)
                    </TableCell>
                    <TableCell className="text-xs">{platformLabel(m)}</TableCell>
                    <TableCell className="font-mono text-xs">{m.platform?.agent_version ?? m.agent_version ?? "—"}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {capabilityLabels(m).length === 0 ? (
                          <span className="text-xs text-muted-foreground">
                            {m.platform ? "None reported" : "Not reported (older agent)"}
                          </span>
                        ) : (
                          capabilityLabels(m).map((label) => (
                            <Badge key={label} variant="outline" className="text-[10px]">
                              {label}
                            </Badge>
                          ))
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={m.availability === "online" ? "success" : m.availability === "throttled" ? "warning" : "outline"}>
                        {m.availability}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Machine Telemetry Drilldown */}
      <Card className="mt-6">
        <CardHeader className="border-b border-border/40 pb-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle className="text-base font-semibold">Computer details</CardTitle>
              <p className="text-xs text-muted-foreground">
                Readings sent by the selected computer, grouped by minute.
              </p>
            </div>
            {machines.length > 0 ? (
              <div className="flex items-center gap-2">
                <label htmlFor="admin-telemetry-computer" className="text-xs text-muted-foreground">Computer:</label>
                <select
                  id="admin-telemetry-computer"
                  value={selectedMachineId}
                  onChange={(e) => setSelectedMachineId(e.target.value)}
                  className="rounded-md border border-input bg-background px-3 py-1.5 text-xs font-mono text-foreground shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  {machines.map((m) => (
                    <option key={m.machine_id} value={m.machine_id}>
                      {m.hostname} ({m.machine_id.slice(0, 8)}…){m.availability === "throttled" ? " - throttled" : ""}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="p-6">
          {!selectedMachineId ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              No computers yet. When a host connects a computer, its readings show up here.
            </div>
          ) : machineTelemetryQuery.isLoading ? (
            <div className="space-y-4">
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          ) : (
            <div>
              {/* Current live snapshot */}
              {machineTelemetry?.current ? (
                <div className="mb-6 grid gap-4 rounded-lg border border-border/60 bg-muted/20 p-4 sm:grid-cols-4">
                  <div>
                    <div className="text-xs text-muted-foreground">GPU use right now</div>
                    <div className="mt-1 text-xl font-bold font-mono">
                      {machineTelemetry.current.gpu_util_pct ?? 0}%
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-muted-foreground">Heat right now</div>
                    <div className="mt-1 text-xl font-bold font-mono text-warning">
                      {machineTelemetry.current.temp_c ?? 0}°C
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-muted-foreground">GPU memory in use</div>
                    <div className="mt-1 text-xl font-bold font-mono">
                      {machineTelemetry.current.mem_used_mb ?? 0} MB
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-muted-foreground">Power use</div>
                    <div className="mt-1 text-xl font-bold font-mono text-primary">
                      {machineTelemetry.current.power_w ?? 0} W
                    </div>
                  </div>
                </div>
              ) : null}

              {/* Time-series rollups */}
              <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                Readings by minute
              </div>
              {machineTelemetry?.points && machineTelemetry.points.length > 0 ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Time (UTC)</TableHead>
                      <TableHead>Average GPU use</TableHead>
                      <TableHead>Highest GPU use</TableHead>
                      <TableHead>Average heat</TableHead>
                      <TableHead>Highest heat</TableHead>
                      <TableHead>Average memory</TableHead>
                      <TableHead>Average power</TableHead>
                      <TableHead>Readings</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {machineTelemetry.points.map((pt, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-mono text-xs">
                          {new Date(pt.timestamp).toLocaleTimeString([], {
                            timeZone: "UTC",
                            hour: "2-digit",
                            minute: "2-digit",
                            second: "2-digit",
                          })}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {pt.avg_gpu_util_pct != null ? `${pt.avg_gpu_util_pct}%` : "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {pt.max_gpu_util_pct != null ? `${pt.max_gpu_util_pct}%` : "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {pt.avg_temp_c != null ? `${pt.avg_temp_c}°C` : "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-warning">
                          {pt.max_temp_c != null ? `${pt.max_temp_c}°C` : "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {pt.avg_mem_used_mb != null ? `${pt.avg_mem_used_mb} MB` : "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {pt.avg_power_w != null ? `${pt.avg_power_w} W` : "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {pt.samples}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <div className="p-8 text-center text-sm text-muted-foreground">
                  No recent readings from this computer. Check that the host has the agent running.
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
