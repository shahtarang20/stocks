"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, FileText, RefreshCw } from "lucide-react";
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
import { api, ApiError, CREDITS_LABEL, type Invoice } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { invoiceStatusVariant, humanizeStatus } from "@/lib/billing";
import { formatRelativeTime } from "@/lib/utils";
import { shortId } from "@/lib/jobs";

export default function InvoicesPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery<Invoice[]>({
    queryKey: ["invoices"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listInvoices(token),
  });

  const invoices = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Invoices"
        description="Itemized invoices per settled job from GET /v1/invoices. Estimated vs actual are tracked separately. Amounts are simulated credits."
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
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </CardContent>
        </Card>
      ) : query.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load invoices</h3>
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
      ) : invoices.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
              <FileText className="h-6 w-6" />
            </span>
            <h3 className="font-semibold">No invoices yet</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              A settled job produces an itemized invoice here. {CREDITS_LABEL}.
            </p>
            <Button asChild>
              <Link href="/dashboard/jobs/new">Submit a job</Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Invoice</TableHead>
                <TableHead>Job</TableHead>
                <TableHead className="text-right">Estimated</TableHead>
                <TableHead className="text-right">Actual</TableHead>
                <TableHead className="text-right">Platform fee</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Date</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {invoices.map((inv) => (
                <TableRow key={inv.invoice_id}>
                  <TableCell>
                    <Link
                      href={`/dashboard/invoices/${inv.invoice_id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {shortId(inv.invoice_id)}
                    </Link>
                  </TableCell>
                  <TableCell>
                    {inv.job_id ? (
                      <Link
                        href={`/dashboard/jobs/${inv.job_id}`}
                        className="font-mono text-xs text-muted-foreground hover:underline"
                      >
                        {shortId(inv.job_id)}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                    {inv.estimated_total}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums font-medium">
                    {inv.actual_total}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                    {inv.platform_fee}
                  </TableCell>
                  <TableCell>
                    <Badge variant={invoiceStatusVariant(inv.status)}>
                      {humanizeStatus(inv.status)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatRelativeTime(inv.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </>
  );
}
