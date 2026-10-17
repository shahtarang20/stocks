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
    queryFn: () => api.listMachines(token),
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
        title="Telemetry & Fleet Health"
        description="Real-time GPU time-series telemetry, thermal & memory metrics, power consumption, and operational threshold alerts."
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
              Active Telemetry GPUs
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
                <span className="text-xs text-muted-foreground">reporting online</span>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Fleet GPU Utilization */}
        <Card className="relative overflow-hidden border-border/60 bg-gradient-to-br from-card to-card/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Fleet Avg Utilization
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
              Avg Core Temp / Power
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
                  <span className="ml-1 text-xs text-muted-foreground">thermal avg</span>
                </div>
                <div className="text-right">
                  <div className="text-sm font-semibold text-foreground">
                    {stats?.fleet_total_power_kw ?? 0} kW
                  </div>
                  <div className="text-xs text-muted-foreground">total draw</div>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Active Alerts */}
        <Card className="relative overflow-hidden border-border/60 bg-gradient-to-br from-card to-card/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Operational Alerts
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

      {/* Operational Alerts Management Table */}
      <Card className="mt-6">
        <CardHeader className="border-b border-border/40 pb-4">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base font-semibold">Live Operational Alerts</CardTitle>
              <p className="text-xs text-muted-foreground">
                Automated threshold violations for GPU temperatures (&gt;85°C) and memory saturation (&gt;95%).
              </p>
            </div>
            <Badge variant={alerts.filter((a) => a.status === "active").length > 0 ? "danger" : "outline"}>
              {alerts.filter((a) => a.status === "active").length} Open Alerts
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
              All monitored machines are operating within normal thermal and resource envelopes.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Severity</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Message</TableHead>
                  <TableHead>Metric / Limit</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Reported</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
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
                          Ack
                        </Button>
                      ) : null}
                      {alert.status !== "resolved" ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => resolveMut.mutate(alert.id)}
                          disabled={resolveMut.isPending}
                        >
                          Resolve
                        </Button>
                      ) : (
                        <span className="text-xs text-muted-foreground">Resolved</span>
                      )}
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
              <CardTitle className="text-base font-semibold">Node Hardware Telemetry Explorer</CardTitle>
              <p className="text-xs text-muted-foreground">
                High-frequency stream rollups with 1-minute bucketed aggregations.
              </p>
            </div>
            {machines.length > 0 ? (
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">Node:</span>
                <select
                  value={selectedMachineId}
                  onChange={(e) => setSelectedMachineId(e.target.value)}
                  className="rounded-md border border-input bg-background px-3 py-1.5 text-xs font-mono text-foreground shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  {machines.map((m) => (
                    <option key={m.machine_id} value={m.machine_id}>
                      {m.hostname} ({m.machine_id.slice(0, 8)}…)
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
              No registered provider nodes found. Enroll a machine agent to begin streaming telemetry.
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
                    <div className="text-xs text-muted-foreground">Current GPU Utilization</div>
                    <div className="mt-1 text-xl font-bold font-mono">
                      {machineTelemetry.current.gpu_util_pct ?? 0}%
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-muted-foreground">Current Temperature</div>
                    <div className="mt-1 text-xl font-bold font-mono text-warning">
                      {machineTelemetry.current.temp_c ?? 0}°C
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-muted-foreground">VRAM In Use</div>
                    <div className="mt-1 text-xl font-bold font-mono">
                      {machineTelemetry.current.mem_used_mb ?? 0} MB
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-muted-foreground">Power Draw</div>
                    <div className="mt-1 text-xl font-bold font-mono text-primary">
                      {machineTelemetry.current.power_w ?? 0} W
                    </div>
                  </div>
                </div>
              ) : null}

              {/* Time-series rollups */}
              <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                1-Minute Aggregated Buckets
              </div>
              {machineTelemetry?.points && machineTelemetry.points.length > 0 ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Time (UTC)</TableHead>
                      <TableHead>Avg GPU Util</TableHead>
                      <TableHead>Max GPU Util</TableHead>
                      <TableHead>Avg Temp</TableHead>
                      <TableHead>Max Temp</TableHead>
                      <TableHead>Avg Memory</TableHead>
                      <TableHead>Avg Power</TableHead>
                      <TableHead>Samples</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {machineTelemetry.points.map((pt, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-mono text-xs">
                          {new Date(pt.timestamp).toLocaleTimeString([], {
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
                  No telemetry stream recorded for this node in the current time window.
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
