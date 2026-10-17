"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, BarChart3, RefreshCw } from "lucide-react";
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
import { formatRelativeTime } from "@/lib/utils";
import { shortId } from "@/lib/jobs";

function SummaryStat({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="p-5">
        <p className="text-sm text-muted-foreground">{label}</p>
        {/* Decimal strings shown verbatim — never parseFloat. */}
        <p className="mt-1 font-mono text-2xl font-bold tabular-nums">{value}</p>
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
  const summary = data?.summary;
  const currency = summary?.currency ?? data?.currency ?? "USDC_CREDITS";

  return (
    <>
      <ConsolePageHeader
        title="Usage"
        description="Per-job metered usage from the billing service — estimated vs actual cost, tracked separately. Amounts are simulated credits, shown verbatim."
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
          <div className="grid gap-4 sm:grid-cols-3">
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
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load usage</h3>
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
      ) : (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-3">
            <SummaryStat
              label="Total spend"
              value={summary?.total_spend ?? summary?.total_actual ?? "0.00"}
            />
            <SummaryStat
              label="Estimated total"
              value={summary?.total_estimated ?? "0.00"}
            />
            <SummaryStat
              label="Jobs metered"
              value={String(summary?.job_count ?? records.length)}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            {currency} · {CREDITS_LABEL}. Estimated and actual are tracked
            separately; a failed or cancelled job is never charged.
          </p>

          <Card>
            <CardHeader>
              <CardTitle>Per-job usage</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {records.length === 0 ? (
                <div className="flex flex-col items-center gap-3 p-12 text-center">
                  <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
                    <BarChart3 className="h-6 w-6" />
                  </span>
                  <h3 className="font-semibold">No usage recorded yet</h3>
                  <p className="max-w-md text-sm text-muted-foreground">
                    Once a job completes and settles, its metered duration and
                    actual cost appear here.
                  </p>
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Job</TableHead>
                      <TableHead className="text-right">Duration</TableHead>
                      <TableHead className="text-right">Estimated</TableHead>
                      <TableHead className="text-right">Actual</TableHead>
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
                              className="font-mono text-xs text-primary hover:underline"
                            >
                              {shortId(r.job_id)}
                            </Link>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums">
                          {formatDuration(r.duration_seconds)}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                          {r.estimated_cost ?? "—"}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums font-medium">
                          {r.actual_cost ?? "—"}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {r.created_at ? formatRelativeTime(r.created_at) : "—"}
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
