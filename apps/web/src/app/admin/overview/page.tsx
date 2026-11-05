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
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { jobTypeLabel } from "@/lib/jobs";
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
    { label: "Hosts online", value: stats ? stats.active_providers.toLocaleString() : "—", icon: Server, note: "online" },
    { label: "Total jobs", value: stats ? stats.total_jobs.toLocaleString() : "—", icon: Activity, note: "live" },
    { label: "Success rate", value: stats ? `${stats.success_rate}%` : "—", icon: CheckCircle2, note: "completion" },
  ];

  return (
    <>
      <ConsolePageHeader
        title="Admin overview"
        description="A quick look at how many people, hosts and jobs are on ComputeCoin. Check that jobs are finishing, then open a page below if something looks wrong."
        action={
          <Button variant="outline" onClick={handleRefresh} disabled={isRefreshing}>
            <RefreshCw className={isRefreshing ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="space-y-6">
        {/* Stats */}
        <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
          {liveStats.map((stat) => (
            <Card key={stat.label}>
              <CardContent className="flex items-start justify-between p-5">
                <div>
                  <p className="text-sm text-muted-foreground">{stat.label}</p>
                  <div className="mt-1 text-2xl font-bold tracking-tight">
                    {statsQuery.isLoading ? <Skeleton className="h-8 w-16" /> : stat.value}
                  </div>
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
            { href: "/admin/providers", label: "Manage hosts" },
            { href: "/admin/jobs", label: "See all jobs" },
            { href: "/admin/disputes", label: "Review disputes" },
            { href: "/admin/audit-logs", label: "Read the audit log" },
            { href: "/admin/configuration", label: "See settings" },
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
            <CardTitle>Latest jobs</CardTitle>
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
                  {jobsQuery.error instanceof ApiError ? jobsQuery.error.message : "We could not load the jobs. Select Refresh to try again."}
                </p>
              </div>
            ) : recentJobs.length === 0 ? (
              <p className="p-6 text-sm text-muted-foreground">No jobs yet. Jobs show up here as soon as a customer sends one.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Job</TableHead>
                    <TableHead>Type of work</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>
                      How it ran
                      <HelpTip term="practice mode">
                        Practice mode means the job was only acted out. The result is a placeholder, not real work.
                      </HelpTip>
                    </TableHead>
                    <TableHead>Created</TableHead>
                    <TableHead>Cost (credits)</TableHead>
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
                        <TableCell className="text-sm">{jobTypeLabel(job.workload_type)}</TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1.5">
                            {statusIcon(job.status)}
                            <span className="text-sm capitalize">{job.status}</span>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant={job.execution_mode === "simulated" ? "warning" : "neutral"}>
                            {(job.execution_mode ?? "simulated") === "simulated" ? "Practice mode" : job.execution_mode}
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
