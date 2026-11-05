"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import {
  api,
  ApiError,
  CREDITS_LABEL,
  type UsageRecord,
  type UsageResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatDuration } from "@/lib/billing";
import { ErrorState, RelativeTime } from "@/components/console/bits";
import { shortId } from "@/lib/jobs";

function SummaryStat({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="p-4 sm:p-5">
        <p className="text-xs text-muted-foreground sm:text-sm">{label}</p>
        {/* Decimal strings shown verbatim — never parseFloat. */}
        <p className="mt-1 truncate font-mono text-xl font-bold tabular-nums sm:text-2xl">{value}</p>
      </CardContent>
    </Card>
  );
}

export default function UsagePage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery<UsageResponse>({
    queryKey: ["usage"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getUsage(token),
  });

  const data = query.data;
  const records: UsageRecord[] =
    data?.usage_records ?? data?.records ?? [];
  // The API returns the totals flat beside the records; older shapes nest them
  // under `summary`. Reading only `summary` left both totals at 0.00.
  const summary = data?.summary ?? {
    total_actual: data?.total_actual,
    total_estimated: data?.total_estimated,
  };
  const currency = summary?.currency ?? data?.currency ?? "USDC_CREDITS";

  return (
    <>
      <ConsolePageHeader
        title="Usage"
        description="How much time each job used and what it cost. The estimate is our guess before the job runs. The actual cost is what you were charged when it finished. Amounts are in test credits."
        action={
          <Button
            variant="outline"
            onClick={() => query.refetch()}
            disabled={query.isFetching || !accessToken}
          >
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {query.isLoading ? (
        <div className="space-y-6">
          <div className="grid grid-cols-3 gap-3 sm:gap-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Card key={i}>
                <CardContent className="space-y-2 p-5">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-8 w-28" />
                </CardContent>
              </Card>
            ))}
          </div>
          <Card>
            <CardContent className="space-y-3 p-6">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </CardContent>
          </Card>
        </div>
      ) : query.isError ? (
        <ErrorState
          title="We could not load your usage"
          message={query.error instanceof ApiError ? query.error.message : undefined}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : (
        <div className="space-y-6">
          <div className="grid grid-cols-3 gap-3 sm:gap-4">
            <SummaryStat
              label="Credits spent"
              value={summary?.total_spend ?? summary?.total_actual ?? "0.00"}
            />
            <SummaryStat
              label="Estimated cost, all jobs"
              value={summary?.total_estimated ?? "0.00"}
            />
            <SummaryStat
              label="Jobs counted"
              value={String(summary?.job_count ?? records.length)}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            {currency === "USDC_CREDITS" ? CREDITS_LABEL : `${currency} · ${CREDITS_LABEL}`}.
            The estimate and the actual cost are kept apart. A failed or cancelled job is never charged.
          </p>

          <Card>
            <CardHeader>
              <CardTitle>Usage for each job</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {records.length === 0 ? (
                <div className="flex flex-col items-center gap-3 p-12 text-center">
                  <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
                    <BarChart3 className="h-6 w-6" />
                  </span>
                  <h3 className="font-semibold">No usage yet</h3>
                  <p className="max-w-md text-sm text-muted-foreground">
                    When a job finishes, you will see here how long it ran and what it cost. To get started, start a job from the Jobs page.
                  </p>
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Job</TableHead>
                      <TableHead className="text-right">Time used</TableHead>
                      <TableHead className="text-right">Estimate</TableHead>
                      <TableHead className="text-right">Charged</TableHead>
                      <TableHead>When</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {records.map((r, i) => (
                      <TableRow key={r.usage_id ?? r.job_id ?? i}>
                        <TableCell>
                          {r.job_id ? (
                            <Link
                              href={`/dashboard/jobs/${r.job_id}`}
                              title={r.job_id}
                              className="font-mono text-xs text-primary hover:underline"
                            >
                              {shortId(r.job_id)}
                            </Link>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums">
                          {formatDuration(r.duration_seconds ?? r.actual_seconds)}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                          {r.estimated_cost ?? "—"}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums font-medium">
                          {r.actual_cost ?? "—"}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          <RelativeTime iso={r.created_at} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </>
  );
}
