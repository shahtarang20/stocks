"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ListChecks,
  Plus,
  RefreshCw,
} from "lucide-react";
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
import { api, ApiError, type Job } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";
import {
  isSimulated,
  jobStatusLabel,
  jobStatusVariant,
  shortId,
} from "@/lib/jobs";

export default function JobsPage() {
  const { accessToken } = useAuth();
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
        description="Compute jobs you have submitted, fetched live from GET /v1/jobs. Completed jobs show their real settled cost; failed or cancelled jobs are never charged."
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
                <Plus /> New job
              </Link>
            </Button>
          </div>
        }
      />

      {query.isLoading ? (
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4">
                <Skeleton className="h-10 w-10 rounded-md" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-1/3" />
                  <Skeleton className="h-3 w-1/2" />
                </div>
                <Skeleton className="h-6 w-16 rounded-full" />
              </div>
            ))}
          </CardContent>
        </Card>
      ) : query.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load jobs</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {query.error instanceof ApiError
                ? query.error.message
                : "An unexpected error occurred."}
            </p>
            <Button variant="outline" onClick={() => query.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : jobs.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
              <ListChecks className="h-6 w-6" />
            </span>
            <h3 className="font-semibold">No jobs yet</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              Submit a text-generation or batch-inference job and track its
              progress through the scheduler and executor here.
            </p>
            <Button asChild>
              <Link href="/dashboard/jobs/new">
                <Plus /> New job
              </Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job</TableHead>
                <TableHead>Workload</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((job) => {
                const id = job.job_id ?? job.id ?? "";
                const simulated = isSimulated(job.execution_mode);
                return (
                  <TableRow
                    key={id}
                    className="cursor-pointer"
                    onClick={() => {
                      window.location.href = `/dashboard/jobs/${id}`;
                    }}
                  >
                    <TableCell>
                      <Link
                        href={`/dashboard/jobs/${id}`}
                        className="font-mono text-xs text-primary hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {shortId(id)}
                      </Link>
                    </TableCell>
                    <TableCell className="text-sm">
                      {job.workload_type}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {job.model}
                    </TableCell>
                    <TableCell>
                      <Badge variant={jobStatusVariant(job.status)}>
                        {jobStatusLabel(job.status)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={simulated ? "warning" : "neutral"}>
                        {simulated ? "Simulated" : "Hardware"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatRelativeTime(job.created_at)}
                    </TableCell>
                    <TableCell className="text-xs">
                      {job.status === "completed" && job.final_cost != null ? (
                        <span className="font-mono tabular-nums text-foreground">
                          {job.final_cost}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">not charged</span>
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
