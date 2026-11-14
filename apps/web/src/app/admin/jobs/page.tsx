"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Activity, CheckCircle2, Clock, RefreshCw, XCircle } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card } from "@/components/ui/card";
import { HelpTip } from "@/components/ui/help-tip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyId, EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { ExecutionModeBadge, JobStatusBadge } from "@/components/jobs/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError, type JobExecutionMode } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { jobTypeLabel, shortId } from "@/lib/jobs";

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
        title="Jobs"
        description="Every job that every customer has sent. Look here when a customer says a job is stuck or failed. The list updates every 10 seconds."
        action={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {query.isLoading ? (
        <TableSkeleton rows={6} label="Loading jobs" />
      ) : query.isError ? (
        <ErrorState
          title="We could not load the jobs"
          message={query.error instanceof ApiError ? query.error.message : undefined}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : jobs.length === 0 ? (
        <EmptyState title="No jobs yet">No customer has sent a job yet. Jobs show up here when they do.</EmptyState>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job</TableHead>
                <TableHead>Customer</TableHead>
                <TableHead>Type of work</TableHead>
                <TableHead className="hidden lg:table-cell">Model</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>
                  How it ran
                  <HelpTip term="how it ran">Practice mode means the job was only acted out and the result is a placeholder. Real hardware means it ran on a host&apos;s computer.</HelpTip>
                </TableHead>
                <TableHead className="hidden xl:table-cell">
                  Privacy level
                  <HelpTip term="privacy level">How strongly the job is kept apart from the host. Standard is a locked-down container. Higher levels need special hardware on the host.</HelpTip>
                </TableHead>
                <TableHead>Created</TableHead>
                <TableHead className="text-right">Cost (credits)</TableHead>
                <TableHead className="hidden lg:table-cell">Problem</TableHead>
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
                        href={`/admin/jobs/${id}`}
                        title={id}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {shortId(id)}
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      <CopyId id={customerId} label="Copy customer id" />
                    </TableCell>
                    <TableCell className="text-sm">{jobTypeLabel(job.workload_type)}</TableCell>
                    <TableCell className="hidden font-mono text-xs text-muted-foreground lg:table-cell">
                      {job.model}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <StatusIcon status={job.status} />
                        <JobStatusBadge status={job.status} />
                      </div>
                    </TableCell>
                    <TableCell>
                      <ExecutionModeBadge mode={(job.execution_mode ?? "simulated") as JobExecutionMode} />
                    </TableCell>
                    <TableCell className="hidden xl:table-cell">
                      <Badge variant="outline" className="font-mono text-xs">
                        {job.isolation_tier || "container"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      <RelativeTime iso={job.created_at} />
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {job.final_cost ?? "—"}
                    </TableCell>
                    <TableCell className="hidden max-w-[120px] truncate text-xs text-danger lg:table-cell" title={job.error_code ?? undefined}>
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
