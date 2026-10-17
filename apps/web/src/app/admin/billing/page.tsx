"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Scale,
  ShieldAlert,
  Undo2,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  type Reconciliation,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { billingErrorMessage } from "@/lib/billing";

const amountRe = /^\d+(\.\d{1,2})?$/;

// --------------------------- Refund form ---------------------------
const refundSchema = z
  .object({
    job_id: z.string().trim().optional(),
    invoice_id: z.string().trim().optional(),
    amount: z
      .string()
      .trim()
      .refine((v) => amountRe.test(v), { message: "Enter a decimal like 5.00" }),
    reason: z.string().trim().min(3, "Give a reason for the audit log"),
  })
  .refine((v) => Boolean(v.job_id) || Boolean(v.invoice_id), {
    message: "Provide a job ID or an invoice ID",
    path: ["job_id"],
  });

type RefundValues = z.infer<typeof refundSchema>;

function RefundForm() {
  const { accessToken } = useAuth();
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<RefundValues>({
    resolver: zodResolver(refundSchema),
    defaultValues: { job_id: "", invoice_id: "", amount: "", reason: "" },
  });

  const mutation = useMutation({
    mutationFn: (v: RefundValues) =>
      api.postRefund(accessToken as string, {
        job_id: v.job_id || undefined,
        invoice_id: v.invoice_id || undefined,
        amount: v.amount,
        reason: v.reason,
      }),
    onSuccess: () => reset({ job_id: "", invoice_id: "", amount: "", reason: "" }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Undo2 className="h-4 w-4 text-muted-foreground" /> Issue a refund
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={handleSubmit((v) => mutation.mutate(v))}
          noValidate
          className="space-y-4"
        >
          {mutation.isSuccess ? (
            <div className="flex items-start gap-2 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
              <span className="text-muted-foreground">
                Refund posted. It reverses the charge as a balanced ledger
                transaction and is recorded in the audit log.
              </span>
            </div>
          ) : null}
          {mutation.isError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{billingErrorMessage(mutation.error)}</span>
            </div>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="refund-job">Job ID</Label>
              <Input id="refund-job" placeholder="job_…" {...register("job_id")} />
              {errors.job_id ? (
                <p className="text-xs text-danger">{errors.job_id.message}</p>
              ) : null}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="refund-invoice">Invoice ID</Label>
              <Input
                id="refund-invoice"
                placeholder="inv_…"
                {...register("invoice_id")}
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Provide either a job ID or an invoice ID to target the charge.
          </p>

          <div className="space-y-1.5">
            <Label htmlFor="refund-amount">Amount</Label>
            <Input
              id="refund-amount"
              inputMode="decimal"
              placeholder="e.g. 5.00"
              aria-invalid={Boolean(errors.amount)}
              {...register("amount")}
            />
            {errors.amount ? (
              <p className="text-xs text-danger">{errors.amount.message}</p>
            ) : (
              <p className="text-xs text-muted-foreground">{CREDITS_LABEL}.</p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="refund-reason">Reason (audited)</Label>
            <Input
              id="refund-reason"
              placeholder="Why this refund is authorized"
              aria-invalid={Boolean(errors.reason)}
              {...register("reason")}
            />
            {errors.reason ? (
              <p className="text-xs text-danger">{errors.reason.message}</p>
            ) : null}
          </div>

          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? (
              <>
                <Loader2 className="animate-spin" /> Posting refund…
              </>
            ) : (
              "Post refund"
            )}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

// --------------------------- Authorize payout ---------------------------
function AuthorizePayoutForm() {
  const { accessToken } = useAuth();
  const [payoutId, setPayoutId] = useState("");

  const mutation = useMutation({
    mutationFn: (id: string) => api.authorizePayout(accessToken as string, id),
    onSuccess: () => setPayoutId(""),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2 className="h-4 w-4 text-muted-foreground" /> Authorize a
          payout
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Settle a requested payout by its ID (payout_settle: debit
          payout_pending, credit external). Audited, authorized role only.
        </p>
        {mutation.isSuccess ? (
          <div className="flex items-start gap-2 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
            <span className="text-muted-foreground">
              Payout authorized and settled.
            </span>
          </div>
        ) : null}
        {mutation.isError ? (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{billingErrorMessage(mutation.error)}</span>
          </div>
        ) : null}
        <div className="space-y-1.5">
          <Label htmlFor="authorize-payout">Payout ID</Label>
          <Input
            id="authorize-payout"
            placeholder="payout_…"
            value={payoutId}
            onChange={(e) => setPayoutId(e.target.value)}
          />
        </div>
        <Button
          disabled={mutation.isPending || payoutId.trim().length < 1}
          onClick={() => mutation.mutate(payoutId.trim())}
        >
          {mutation.isPending ? (
            <>
              <Loader2 className="animate-spin" /> Authorizing…
            </>
          ) : (
            "Authorize payout"
          )}
        </Button>
      </CardContent>
    </Card>
  );
}

// --------------------------- Resolve dispute ---------------------------
const resolveSchema = z.object({
  dispute_id: z.string().trim().min(1, "Enter a dispute ID"),
  resolution: z.string().trim().min(3, "Describe the resolution"),
  refund_amount: z
    .string()
    .trim()
    .refine((v) => v === "" || amountRe.test(v), {
      message: "Enter a decimal like 5.00 (or leave blank)",
    }),
});

type ResolveValues = z.infer<typeof resolveSchema>;

function ResolveDisputeForm() {
  const { accessToken } = useAuth();
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<ResolveValues>({
    resolver: zodResolver(resolveSchema),
    defaultValues: { dispute_id: "", resolution: "", refund_amount: "" },
  });

  const mutation = useMutation({
    mutationFn: (v: ResolveValues) =>
      api.resolveDispute(accessToken as string, v.dispute_id, {
        resolution: v.resolution,
        refund_amount: v.refund_amount || undefined,
      }),
    onSuccess: () =>
      reset({ dispute_id: "", resolution: "", refund_amount: "" }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-muted-foreground" /> Resolve a
          dispute
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={handleSubmit((v) => mutation.mutate(v))}
          noValidate
          className="space-y-4"
        >
          {mutation.isSuccess ? (
            <div className="flex items-start gap-2 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
              <span className="text-muted-foreground">
                Dispute resolved and recorded in the audit log.
              </span>
            </div>
          ) : null}
          {mutation.isError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{billingErrorMessage(mutation.error)}</span>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="resolve-id">Dispute ID</Label>
            <Input
              id="resolve-id"
              placeholder="dispute_…"
              aria-invalid={Boolean(errors.dispute_id)}
              {...register("dispute_id")}
            />
            {errors.dispute_id ? (
              <p className="text-xs text-danger">{errors.dispute_id.message}</p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="resolve-resolution">Resolution</Label>
            <Input
              id="resolve-resolution"
              placeholder="e.g. upheld — partial refund issued"
              aria-invalid={Boolean(errors.resolution)}
              {...register("resolution")}
            />
            {errors.resolution ? (
              <p className="text-xs text-danger">{errors.resolution.message}</p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="resolve-refund">Refund amount (optional)</Label>
            <Input
              id="resolve-refund"
              inputMode="decimal"
              placeholder="e.g. 5.00"
              aria-invalid={Boolean(errors.refund_amount)}
              {...register("refund_amount")}
            />
            {errors.refund_amount ? (
              <p className="text-xs text-danger">
                {errors.refund_amount.message}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Leave blank to resolve without a refund. {CREDITS_LABEL}.
              </p>
            )}
          </div>

          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? (
              <>
                <Loader2 className="animate-spin" /> Resolving…
              </>
            ) : (
              "Resolve dispute"
            )}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

// --------------------------- Reconciliation ---------------------------
function ReconciliationPanel() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery<Reconciliation>({
    queryKey: ["reconciliation"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getReconciliation(token),
  });

  const data = query.data;
  const forbidden =
    query.error instanceof ApiError &&
    (query.error.status === 403 || query.error.status === 401);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2">
          <Scale className="h-4 w-4 text-muted-foreground" /> Reconciliation
        </CardTitle>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => query.refetch()}
          disabled={query.isFetching || !accessToken}
        >
          <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </CardHeader>
      <CardContent>
        {query.isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-8 w-40" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : query.isError ? (
          <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              {forbidden
                ? "Your account does not have admin access. Authorization is enforced server-side (RBAC)."
                : query.error instanceof ApiError
                  ? query.error.message
                  : "Couldn't load reconciliation."}
            </span>
          </div>
        ) : data ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <Badge variant={data.balanced ? "success" : "danger"}>
                {data.balanced ? "Balanced" : "IMBALANCED"}
              </Badge>
              <span className="text-sm text-muted-foreground">
                Imbalance:{" "}
                <span className="font-mono tabular-nums text-foreground">
                  {data.imbalance}
                </span>
              </span>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="rounded-md border border-border p-4">
                <p className="text-sm text-muted-foreground">Total debits</p>
                <p className="mt-1 font-mono text-xl font-semibold tabular-nums">
                  {data.total_debits}
                </p>
              </div>
              <div className="rounded-md border border-border p-4">
                <p className="text-sm text-muted-foreground">Total credits</p>
                <p className="mt-1 font-mono text-xl font-semibold tabular-nums">
                  {data.total_credits}
                </p>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              INVARIANT 2 — across the whole ledger, total debits must equal total
              credits (imbalance 0.00). {CREDITS_LABEL}.
            </p>

            {data.accounts && data.accounts.length > 0 ? (
              <div className="overflow-hidden rounded-md border border-border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Account</TableHead>
                      <TableHead>Owner</TableHead>
                      <TableHead>Type</TableHead>
                      <TableHead className="text-right">Balance</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.accounts.map((a, i) => (
                      <TableRow key={a.account_id ?? i}>
                        <TableCell className="font-mono text-xs">
                          {a.account_id ?? "—"}
                        </TableCell>
                        <TableCell className="text-sm">
                          {a.owner_type ?? "—"}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {a.type ?? "—"}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums">
                          {a.balance ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

export default function AdminBillingPage() {
  const { isAdmin } = useAuth();

  return (
    <>
      <ConsolePageHeader
        title="Billing & reconciliation"
        description="Operator tools: ledger reconciliation, refunds, payout authorization and dispute resolution. Every money move is a balanced, audited ledger transaction."
      />

      <div className="space-y-6">
        {!isAdmin ? (
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            <span className="text-muted-foreground">
              <span className="font-medium text-foreground">
                This is an operator surface.
              </span>{" "}
              Your token doesn&apos;t appear to carry an admin role. This client
              check is a UX convenience only — the API enforces RBAC server-side
              and will reject unauthorized calls below.
            </span>
          </div>
        ) : null}

        <ReconciliationPanel />
        <RefundForm />
        <div className="grid gap-6 lg:grid-cols-2">
          <AuthorizePayoutForm />
          <ResolveDisputeForm />
        </div>
      </div>
    </>
  );
}
