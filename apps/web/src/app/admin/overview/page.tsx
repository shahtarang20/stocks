"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Clock,
  RefreshCw,
  Server,
  Users,
  Wallet,
  XCircle,
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
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";

function statusIcon(status: string) {
  if (status === "completed") return <CheckCircle2 className="h-4 w-4 text-success" />;
  if (status === "failed" || status === "cancelled") return <XCircle className="h-4 w-4 text-danger" />;
  if (status === "running") return <Activity className="h-4 w-4 text-primary animate-pulse" />;
  return <Clock className="h-4 w-4 text-muted-foreground" />;
}

export default function AdminOverviewPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const statsQuery = useQuery({
    queryKey: ["admin", "stats"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getAdminPlatformStats(token),
    refetchInterval: 15000,
  });

  const jobsQuery = useQuery({
    queryKey: ["admin", "jobs-recent"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listAllAdminJobs(token, 15),
    refetchInterval: 15000,
  });

  const stats = statsQuery.data;
  const recentJobs = jobsQuery.data ?? [];

  const isRefreshing = statsQuery.isFetching || jobsQuery.isFetching;

  function handleRefresh() {
    statsQuery.refetch();
    jobsQuery.refetch();
  }

  const liveStats = [
    { label: "Total users", value: stats ? stats.total_users.toLocaleString() : "—", icon: Users, note: "live" },
    { label: "Active providers", value: stats ? stats.active_providers.toLocaleString() : "—", icon: Server, note: "online" },
    { label: "Total jobs", value: stats ? stats.total_jobs.toLocaleString() : "—", icon: Activity, note: "live" },
    { label: "Success rate", value: stats ? `${stats.success_rate}%` : "—", icon: CheckCircle2, note: "completion" },
  ];

  return (
    <>
      <ConsolePageHeader
        title="Admin overview"
        description="Platform-wide operational telemetry fetched live from GET /v1/admin/stats and GET /v1/admin/jobs."
        action={
          <Button variant="outline" onClick={handleRefresh} disabled={isRefreshing}>
            <RefreshCw className={isRefreshing ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="space-y-6">
        {/* Stats */}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {liveStats.map((stat) => (
            <Card key={stat.label}>
              <CardContent className="flex items-start justify-between p-5">
                <div>
                  <p className="text-sm text-muted-foreground">{stat.label}</p>
                  <p className="mt-1 text-2xl font-bold tracking-tight">
                    {statsQuery.isLoading ? <Skeleton className="h-8 w-16" /> : stat.value}
                  </p>
                  <Badge variant="success" className="mt-2 text-xs">{stat.note}</Badge>
                </div>
                <span className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <stat.icon className="h-4 w-4" />
                </span>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Quick links */}
        <div className="grid gap-3 sm:grid-cols-3">
          {[
            { href: "/admin/users", label: "Manage users" },
            { href: "/admin/providers", label: "Manage providers" },
            { href: "/admin/jobs", label: "All jobs" },
            { href: "/admin/disputes", label: "Open disputes" },
            { href: "/admin/audit-logs", label: "Audit logs" },
            { href: "/admin/configuration", label: "Configuration" },
          ].map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className="rounded-lg border border-border bg-surface/50 px-4 py-3 text-sm font-medium transition-colors hover:bg-surface-2 hover:text-foreground"
            >
              {l.label} →
            </Link>
          ))}
        </div>

        {/* Recent jobs */}
        <Card>
          <CardHeader>
            <CardTitle>Recent jobs (live)</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {jobsQuery.isLoading ? (
              <div className="space-y-3 p-6">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-8 w-full" />
                ))}
              </div>
            ) : jobsQuery.isError ? (
              <div className="flex flex-col items-center gap-3 p-10 text-center">
                <AlertCircle className="h-8 w-8 text-danger" />
                <p className="text-sm text-muted-foreground">
                  {jobsQuery.error instanceof ApiError ? jobsQuery.error.message : "Failed to load jobs"}
                </p>
              </div>
            ) : recentJobs.length === 0 ? (
              <p className="p-6 text-sm text-muted-foreground">No jobs found across the network.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Job ID</TableHead>
                    <TableHead>Workload</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Mode</TableHead>
                    <TableHead>Created</TableHead>
                    <TableHead>Cost</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {recentJobs.map((job) => {
                    const id = job.job_id ?? (job as { id?: string }).id ?? "";
                    return (
                      <TableRow key={id}>
                        <TableCell>
                          <Link
                            href={`/dashboard/jobs/${id}`}
                            className="font-mono text-xs text-primary hover:underline"
                          >
                            {id.slice(0, 8)}…
                          </Link>
                        </TableCell>
                        <TableCell className="text-sm">{job.workload_type}</TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1.5">
                            {statusIcon(job.status)}
                            <span className="text-sm capitalize">{job.status}</span>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant={job.execution_mode === "simulated" ? "warning" : "neutral"}>
                            {job.execution_mode ?? "simulated"}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {formatRelativeTime(job.created_at)}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {job.final_cost ?? "—"}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
