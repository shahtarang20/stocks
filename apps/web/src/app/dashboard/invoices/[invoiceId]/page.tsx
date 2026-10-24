"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  Download,
  Loader2,
  Receipt,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiAbsoluteUrl, ApiError, CREDITS_LABEL, type Invoice } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { invoiceStatusVariant, humanizeStatus } from "@/lib/billing";
import { formatRelativeTime } from "@/lib/utils";
import { shortId } from "@/lib/jobs";

function Row({
  label,
  value,
  strong,
  muted,
}: {
  label: string;
  value: string;
  strong?: boolean;
  muted?: boolean;
}) {
  return (
    <div
      className={
        strong
          ? "flex items-center justify-between border-t border-border pt-3 text-base font-semibold"
          : "flex items-center justify-between text-sm"
      }
    >
      <span className={strong ? "text-foreground" : "text-muted-foreground"}>
        {label}
      </span>
      {/* Decimal strings shown verbatim — never parseFloat. */}
      <span
        className={
          muted
            ? "font-mono tabular-nums text-muted-foreground"
            : "font-mono tabular-nums"
        }
      >
        {value}
      </span>
    </div>
  );
}

export default function InvoiceDetailPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const params = useParams<{ invoiceId: string }>();
  const invoiceId = params.invoiceId;

  const query = useQuery<Invoice>({
    queryKey: ["invoice", invoiceId],
    enabled: Boolean(accessToken) && Boolean(invoiceId),
    queryFn: () => api.getInvoice(token, invoiceId),
  });

  const downloadMut = useMutation({
    mutationFn: () => api.getInvoiceDownloadUrl(token, invoiceId),
    onSuccess: (res) => {
      if (res?.url) {
        window.open(apiAbsoluteUrl(res.url), "_blank", "noopener,noreferrer");
        return;
      }
      // /v1/invoices/{id}/download returns the invoice document itself, not a
      // link, so there was nothing to open and the button did nothing at all.
      const blob = new Blob([JSON.stringify(res, null, 2)], { type: "application/json" });
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = `invoice-${invoiceId}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(href);
    },
  });

  const invoice = query.data;

  if (query.isLoading) {
    return (
      <>
        <ConsolePageHeader title="Invoice" />
        <Card>
          <CardContent className="space-y-4 p-6">
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-4 w-64" />
            <Skeleton className="h-40 w-full" />
          </CardContent>
        </Card>
      </>
    );
  }

  if (query.isError || !invoice) {
    const notFound =
      query.error instanceof ApiError && query.error.status === 404;
    return (
      <>
        <ConsolePageHeader title="Invoice" />
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">
              {notFound ? "Invoice not found" : "Couldn't load this invoice"}
            </h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {notFound
                ? "This invoice doesn't exist, or it belongs to another account."
                : query.error instanceof ApiError
                  ? query.error.message
                  : "An unexpected error occurred."}
            </p>
            <Button asChild variant="outline">
              <Link href="/dashboard/invoices">
                <ArrowLeft /> Back to invoices
              </Link>
            </Button>
          </CardContent>
        </Card>
      </>
    );
  }

  const currency = invoice.currency ?? "USDC_CREDITS";
  const lineItems = invoice.line_items ?? [];

  return (
    <>
      <ConsolePageHeader
        title={`Invoice ${shortId(invoice.invoice_id)}`}
        description="Itemized invoice for a settled job. Amounts are simulated credits, shown verbatim."
        action={
          <div className="flex gap-2">
            <Button asChild variant="outline">
              <Link href="/dashboard/invoices">
                <ArrowLeft /> Back
              </Link>
            </Button>
            <Button
              onClick={() => downloadMut.mutate()}
              disabled={downloadMut.isPending}
            >
              {downloadMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Linking…
                </>
              ) : (
                <>
                  <Download /> Download
                </>
              )}
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        <Card>
          <CardContent className="p-6">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={invoiceStatusVariant(invoice.status)}>
                {humanizeStatus(invoice.status)}
              </Badge>
              <Badge variant="neutral">{CREDITS_LABEL}</Badge>
              <span className="font-mono text-xs text-muted-foreground">
                {invoice.invoice_id}
              </span>
            </div>

            <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">Job</dt>
                <dd className="mt-0.5">
                  {invoice.job_id ? (
                    <Link
                      href={`/dashboard/jobs/${invoice.job_id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {invoice.job_id}
                    </Link>
                  ) : (
                    "—"
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Issued</dt>
                <dd className="mt-0.5">{formatRelativeTime(invoice.created_at)}</dd>
              </div>
            </dl>

            {downloadMut.isError ? (
              <div
                role="alert"
                className="mt-4 flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  {downloadMut.error instanceof ApiError
                    ? downloadMut.error.message
                    : "Couldn't get a download link for this invoice."}
                </span>
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="space-y-3 p-6">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Receipt className="h-4 w-4 text-muted-foreground" /> Breakdown
            </div>
            {lineItems.length > 0
              ? lineItems.map((li, i) => (
                  <Row key={i} label={li.label} value={li.amount} />
                ))
              : null}
            <Row label="Estimated total" value={invoice.estimated_total} muted />
            <Row label="Platform fee" value={invoice.platform_fee} muted />
            <Row label="Actual total" value={invoice.actual_total} strong />
            <p className="pt-1 text-xs text-muted-foreground">
              {currency} · {CREDITS_LABEL}. Estimated and actual are tracked
              separately; you are billed the actual metered amount, capped at the
              authorized hold.
            </p>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
