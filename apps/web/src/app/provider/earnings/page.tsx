"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, RefreshCw, Wallet } from "lucide-react";
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
import { formatRelativeTime } from "@/lib/utils";

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
  const currency = earnings?.currency ?? "USDC_CREDITS";

  return (
    <>
      <ConsolePageHeader
        title="Earnings"
        description="Live accrued earnings from the double-entry ledger as jobs settle on your hardware. Amounts are simulated credits, shown verbatim."
        action={
          <div className="flex gap-2">
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
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load earnings</h3>
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
            <EarningStat label="Available" value={earnings?.available ?? "0.00"} />
            <EarningStat
              label="Lifetime"
              value={earnings?.lifetime ?? earnings?.available ?? "0.00"}
            />
            <Card>
              <CardContent className="p-5">
                <p className="text-sm text-muted-foreground">Currency</p>
                <p className="mt-1 text-2xl font-bold">{currency}</p>
              </CardContent>
            </Card>
          </div>
          <p className="text-xs text-muted-foreground">
            {CREDITS_LABEL}. Earnings accrue to your provider_earnings account as
            each job settles. Request a payout to move available credits out.
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
                    When a customer job settles on your hardware, your share of the
                    capture appears here.
                  </p>
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Kind</TableHead>
                      <TableHead>Direction</TableHead>
                      <TableHead className="text-right">Amount</TableHead>
                      <TableHead>Reference</TableHead>
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
                              {e.direction}
                            </Badge>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums">
                          {e.amount}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {e.reference ?? e.job_id ?? "—"}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {e.created_at ? formatRelativeTime(e.created_at) : "—"}
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
