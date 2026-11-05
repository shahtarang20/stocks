"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ListChecks, Plus, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { CopyButton, EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { ExecutionModeBadge, JobStatusBadge } from "@/components/jobs/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError, type Job } from "@/lib/api";
import { jobTypeLabel, shortId } from "@/lib/jobs";
import { useAuth } from "@/lib/auth-context";

export default function JobsPage() {
  const { accessToken } = useAuth();
  const router = useRouter();
  const token = accessToken as string;

  const query = useQuery<Job[]>({
    queryKey: ["jobs"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listJobs(token),
    // Refresh the list periodically so active jobs advance without a manual reload.
    refetchInterval: 8000,
  });

  const jobs = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Jobs"
        description="Every job you have started, newest first. A job is one piece of work you send to be run. You only pay for jobs that finish. Failed or cancelled jobs are not charged."
        action={
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => query.refetch()}
              disabled={query.isFetching}
            >
              <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
            <Button asChild>
              <Link href="/dashboard/jobs/new">
                <Plus /> Start a new job
              </Link>
            </Button>
          </div>
        }
      />

      {query.isLoading ? (
        <TableSkeleton rows={4} label="Loading jobs" />
      ) : query.isError ? (
        <ErrorState
          title="We could not load your jobs"
          message={query.error instanceof ApiError ? query.error.message : undefined}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : jobs.length === 0 ? (
        <EmptyState
          icon={ListChecks}
          title="You have not run any jobs yet"
          action={
            <Button asChild>
              <Link href="/dashboard/jobs/new">
                <Plus /> Start your first job
              </Link>
            </Button>
          }
        >
          Start a job and you can follow it here. First, make sure you have credits on the
          Credits and billing page. Jobs that run in practice mode are marked, and they only use
          test credits.
        </EmptyState>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job</TableHead>
                <TableHead className="hidden md:table-cell">Type of job</TableHead>
                <TableHead className="hidden lg:table-cell">AI model</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden sm:table-cell">How it ran</TableHead>
                <TableHead className="hidden sm:table-cell">Created</TableHead>
                <TableHead className="text-right">Cost in credits</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((job) => {
                const id = job.job_id ?? job.id ?? "";
                return (
                  <TableRow
                    key={id}
                    className="cursor-pointer"
                    onClick={() => {
                      router.push(`/dashboard/jobs/${id}`);
                    }}
                  >
                    <TableCell>
                      <div className="flex flex-col gap-1">
                        <span className="inline-flex items-center gap-1">
                          <Link
                            href={`/dashboard/jobs/${id}`}
                            title={id}
                            className="font-mono text-xs text-primary hover:underline"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {shortId(id)}
                          </Link>
                          <CopyButton value={id} label="Copy job id" />
                        </span>
                        {/* Secondary details fold under the id on narrow screens. */}
                        <span className="text-xs text-muted-foreground md:hidden">
                          {jobTypeLabel(job.workload_type)}
                        </span>
                        <span className="sm:hidden">
                          <ExecutionModeBadge mode={job.execution_mode} />
                        </span>
                        <span className="text-xs text-muted-foreground sm:hidden">
                          <RelativeTime iso={job.created_at} />
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="hidden text-sm md:table-cell">
                      {jobTypeLabel(job.workload_type)}
                    </TableCell>
                    <TableCell className="hidden font-mono text-xs text-muted-foreground lg:table-cell">
                      {job.model}
                    </TableCell>
                    <TableCell>
                      <JobStatusBadge status={job.status} />
                    </TableCell>
                    <TableCell className="hidden sm:table-cell">
                      <ExecutionModeBadge mode={job.execution_mode} />
                    </TableCell>
                    <TableCell className="hidden text-sm text-muted-foreground sm:table-cell">
                      <RelativeTime iso={job.created_at} />
                    </TableCell>
                    <TableCell className="text-right text-xs">
                      {job.final_cost != null ? (
                        <span className="font-mono tabular-nums text-foreground">
                          {job.final_cost}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">not charged yet</span>
                      )}
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
