"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Wallet } from "lucide-react";
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
import {
  api,
  ApiError,
  CREDITS_LABEL,
  type EarningEntry,
  type ProviderEarnings,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { ledgerDirectionVariant, humanizeStatus } from "@/lib/billing";
import { CopyId, ErrorState, RelativeTime } from "@/components/console/bits";
import { capitalize, creditsName } from "@/lib/format";

function EarningStat({ label, value }: { label: string; value: string }) {
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

export default function ProviderEarningsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery<ProviderEarnings>({
    queryKey: ["provider-earnings"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getProviderEarnings(token),
  });

  const earnings = query.data;
  const recent: EarningEntry[] = earnings?.recent ?? [];
  const currency = creditsName(earnings?.currency);

  return (
    <>
      <ConsolePageHeader
        title="Earnings"
        description="What your computers have earned. You earn when a customer job finishes, not when it starts or fails. Amounts are test credits, not real money."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => query.refetch()}
              disabled={query.isFetching || !accessToken}
            >
              <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
            <Button asChild>
              <Link href="/provider/payouts">Request payout</Link>
            </Button>
          </div>
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
        <ErrorState
          title="We could not load your earnings"
          message={query.error instanceof ApiError ? query.error.message : undefined}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-3">
            <EarningStat label="Ready to pay out" value={earnings?.available ?? "0.00"} />
            <EarningStat
              label="Total earned so far"
              value={earnings?.lifetime ?? earnings?.available ?? "0.00"}
            />
            <Card>
              <CardContent className="p-5">
                <p className="text-sm text-muted-foreground">Unit</p>
                <p className="mt-1 break-all text-xl font-bold sm:text-2xl">{currency}</p>
              </CardContent>
            </Card>
          </div>
          <p className="text-xs text-muted-foreground">
            {CREDITS_LABEL}. Your earnings are added when each job finishes. To take credits
            out, ask for a payout on the Payouts page. A person approves it.
          </p>

          <Card>
            <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
              <CardTitle>Recent earnings</CardTitle>
              <Badge variant="neutral">{CREDITS_LABEL}</Badge>
            </CardHeader>
            <CardContent className="p-0">
              {recent.length === 0 ? (
                <div className="flex flex-col items-center gap-3 p-12 text-center">
                  <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
                    <Wallet className="h-6 w-6" />
                  </span>
                  <h3 className="font-semibold">No earnings yet</h3>
                  <p className="max-w-md text-sm text-muted-foreground">
                    When a customer job finishes on your computer, your share appears here.
                    To get jobs, your computer must be online and have a price. Check both on the
                    My computers page.
                  </p>
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>What for</TableHead>
                      <TableHead>Money in or out</TableHead>
                      <TableHead className="text-right">Amount (credits)</TableHead>
                      <TableHead className="hidden md:table-cell">Job or note</TableHead>
                      <TableHead>When</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {recent.map((e, i) => (
                      <TableRow key={e.entry_id ?? i}>
                        <TableCell className="text-sm">
                          {humanizeStatus(e.kind ?? "earning")}
                        </TableCell>
                        <TableCell>
                          {e.direction ? (
                            <Badge variant={ledgerDirectionVariant(e.direction)}>
                              {capitalize(e.direction)}
                            </Badge>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums">
                          {e.amount}
                        </TableCell>
                        <TableCell className="hidden text-muted-foreground md:table-cell">
                          <CopyId id={e.reference ?? e.job_id} length={12} label="Copy reference" />
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          <RelativeTime iso={e.created_at} />
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
