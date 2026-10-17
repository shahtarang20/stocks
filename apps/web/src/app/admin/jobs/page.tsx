"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Activity, AlertCircle, CheckCircle2, Clock, RefreshCw, XCircle } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
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
import { jobStatusLabel, jobStatusVariant, shortId } from "@/lib/jobs";

function StatusIcon({ status }: { status: string }) {
  if (status === "completed") return <CheckCircle2 className="h-4 w-4 text-success" />;
  if (status === "failed" || status === "cancelled") return <XCircle className="h-4 w-4 text-danger" />;
  if (status === "running") return <Activity className="h-4 w-4 text-primary animate-pulse" />;
  return <Clock className="h-4 w-4 text-muted-foreground" />;
}

export default function AdminJobsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery({
    queryKey: ["admin", "all-jobs"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listAllAdminJobs(token),
    refetchInterval: 10000,
  });

  const jobs = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="All jobs"
        description="Platform-wide job list fetched live from GET /v1/admin/jobs across all users in the network."
        action={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {query.isLoading ? (
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </CardContent>
        </Card>
      ) : query.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <AlertCircle className="h-8 w-8 text-danger" />
            <p className="text-sm text-muted-foreground">
              {query.error instanceof ApiError ? query.error.message : "Failed to load jobs"}
            </p>
            <Button variant="outline" onClick={() => query.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : jobs.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="p-12 text-center text-sm text-muted-foreground">
            No jobs found across the platform.
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job ID</TableHead>
                <TableHead>Customer</TableHead>
                <TableHead>Workload</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Cost</TableHead>
                <TableHead>Error</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((job) => {
                const id = job.job_id ?? (job as { id?: string }).id ?? "";
                const customerId = (job as { customer_id?: string }).customer_id;
                return (
                  <TableRow key={id}>
                    <TableCell>
                      <Link
                        href={`/dashboard/jobs/${id}`}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {shortId(id)}
                      </Link>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {customerId ? `${customerId.slice(0, 8)}…` : "—"}
                    </TableCell>
                    <TableCell className="text-sm">{job.workload_type}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {job.model}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <StatusIcon status={job.status} />
                        <Badge variant={jobStatusVariant(job.status)}>
                          {jobStatusLabel(job.status)}
                        </Badge>
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
                    <TableCell className="max-w-[120px] truncate text-xs text-danger">
                      {job.error_code ?? "—"}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Card>
      )}
    </>
  );
}
