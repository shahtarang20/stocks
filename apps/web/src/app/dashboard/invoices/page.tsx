"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { FileText, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
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
import { invoiceStatusVariant, invoiceStatusText } from "@/lib/billing";
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
        description="One invoice for each job that finished. It shows what we estimated, what you were charged, and the ComputeCoin fee. Amounts are in test credits."
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
        <TableSkeleton rows={4} label="Loading invoices" />
      ) : query.isError ? (
        <ErrorState
          title="We could not load your invoices"
          message={query.error instanceof ApiError ? query.error.message : undefined}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : invoices.length === 0 ? (
        <EmptyState
          icon={FileText}
          title="You have no invoices yet"
          action={
            <Button asChild>
              <Link href="/dashboard/jobs/new">Start a job</Link>
            </Button>
          }
        >
          When a job finishes, its invoice appears here. {CREDITS_LABEL}.
        </EmptyState>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Invoice</TableHead>
                <TableHead>Job</TableHead>
                <TableHead className="hidden text-right sm:table-cell">Estimate</TableHead>
                <TableHead className="text-right">Charged (credits)</TableHead>
                <TableHead className="hidden text-right md:table-cell">ComputeCoin fee</TableHead>
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
                      title={inv.invoice_id}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {shortId(inv.invoice_id)}
                    </Link>
                  </TableCell>
                  <TableCell>
                    {inv.job_id ? (
                      <Link
                        href={`/dashboard/jobs/${inv.job_id}`}
                        title={inv.job_id}
                        className="font-mono text-xs text-muted-foreground hover:underline"
                      >
                        {shortId(inv.job_id)}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="hidden text-right font-mono tabular-nums text-muted-foreground sm:table-cell">
                    {inv.estimated_total}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums font-medium">
                    {inv.actual_total}
                  </TableCell>
                  <TableCell className="hidden text-right font-mono tabular-nums text-muted-foreground md:table-cell">
                    {inv.platform_fee}
                  </TableCell>
                  <TableCell>
                    <Badge variant={invoiceStatusVariant(inv.status)}>
                      {invoiceStatusText(inv.status)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    <RelativeTime iso={inv.created_at} />
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
