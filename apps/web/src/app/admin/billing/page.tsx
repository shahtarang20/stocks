"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { HelpTip } from "@/components/ui/help-tip";
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
      .refine((v) => amountRe.test(v), { message: "Enter an amount with up to 2 decimals, like 5.00." }),
    reason: z.string().trim().min(3, "Write a short reason (at least 3 letters). It is saved in the audit log."),
  })
  .refine((v) => Boolean(v.job_id) || Boolean(v.invoice_id), {
    message: "Enter a job ID or an invoice ID so we know which charge to refund.",
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

  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (v: RefundValues) =>
      api.postRefund(accessToken as string, {
        job_id: v.job_id || undefined,
        invoice_id: v.invoice_id || undefined,
        amount: v.amount,
        reason: v.reason,
      }),
    onSuccess: () => {
      reset({ job_id: "", invoice_id: "", amount: "", reason: "" });
      // Money moved: the books check and the jobs/disputes lists are now out of date.
      void queryClient.invalidateQueries({ queryKey: ["reconciliation"] });
      void queryClient.invalidateQueries({ queryKey: ["admin"] });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Undo2 className="h-4 w-4 text-muted-foreground" /> Refund a charge
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
                Refund sent. The credits went back to the customer, and the refund is saved in the audit log.
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
              <Label htmlFor="refund-job">Job ID (from the Jobs page)</Label>
              <Input id="refund-job" placeholder="job_…" {...register("job_id")} />
              {errors.job_id ? (
                <p className="text-xs text-danger">{errors.job_id.message}</p>
              ) : null}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="refund-invoice">Invoice ID (or use the job ID)</Label>
              <Input
                id="refund-invoice"
                placeholder="inv_…"
                {...register("invoice_id")}
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Fill in a job ID or an invoice ID. You do not need both.
          </p>

          <div className="space-y-1.5">
            <Label htmlFor="refund-amount">Amount to refund</Label>
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
            <Label htmlFor="refund-reason">Reason (saved in the audit log)</Label>
            <Input
              id="refund-reason"
              placeholder="Why you are refunding this charge"
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
                <Loader2 className="animate-spin" /> Sending refund…
              </>
            ) : (
              "Refund this charge"
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

  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (id: string) => api.authorizePayout(accessToken as string, id),
    onSuccess: () => {
      setPayoutId("");
      void queryClient.invalidateQueries({ queryKey: ["reconciliation"] });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2 className="h-4 w-4 text-muted-foreground" /> Approve a
          payout
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          A host asks for a payout when they want to cash out their earnings. Paste the payout ID here to approve it. Only admins can do this, and it is saved in the audit log.
        </p>
        {mutation.isSuccess ? (
          <div className="flex items-start gap-2 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
            <span className="text-muted-foreground">
              Payout approved.
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
          <Label htmlFor="authorize-payout">Payout ID (from the host&apos;s request)</Label>
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
              <Loader2 className="animate-spin" /> Approving…
            </>
          ) : (
            "Approve payout"
          )}
        </Button>
      </CardContent>
    </Card>
  );
}

// --------------------------- Resolve dispute ---------------------------
const resolveSchema = z.object({
  dispute_id: z.string().trim().min(1, "Enter the dispute ID so we know which dispute to close."),
  resolution: z.string().trim().min(3, "Write a short note on how you decided (at least 3 letters)."),
  refund_amount: z
    .string()
    .trim()
    .refine((v) => v === "" || amountRe.test(v), {
      message: "Enter an amount with up to 2 decimals, like 5.00, or leave it empty.",
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

  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (v: ResolveValues) =>
      api.resolveDispute(accessToken as string, v.dispute_id, {
        resolution: v.resolution,
        refund_amount: v.refund_amount || undefined,
      }),
    onSuccess: () => {
      reset({ dispute_id: "", resolution: "", refund_amount: "" });
      // A dispute may carry a refund: refresh the books check and the disputes list.
      void queryClient.invalidateQueries({ queryKey: ["reconciliation"] });
      void queryClient.invalidateQueries({ queryKey: ["admin"] });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-muted-foreground" /> Close a
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
                Dispute closed and saved in the audit log.
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
            <Label htmlFor="resolve-id">Dispute ID (from the Disputes page)</Label>
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
            <Label htmlFor="resolve-resolution">Your decision</Label>
            <Input
              id="resolve-resolution"
              placeholder="e.g. customer was right, part refunded"
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
                Leave empty to close the dispute without a refund. {CREDITS_LABEL}.
              </p>
            )}
          </div>

          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? (
              <>
                <Loader2 className="animate-spin" /> Closing…
              </>
            ) : (
              "Close dispute"
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
          <Scale className="h-4 w-4 text-muted-foreground" /> Books check
          <HelpTip term="books check">
            Every credit that leaves one account must arrive in another. This check adds up all money movements and makes sure nothing is missing or extra.
          </HelpTip>
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
                ? "Your account does not have admin access, so this check cannot be shown. Ask an admin to give you access."
                : query.error instanceof ApiError
                  ? query.error.message
                  : "We could not load the books check. Select Refresh to try again."}
            </span>
          </div>
        ) : data ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <Badge variant={data.balanced ? "success" : "danger"}>
                {data.balanced ? "Balanced" : "Does not add up"}
              </Badge>
              <span className="text-sm text-muted-foreground">
                Difference:{" "}
                <span className="font-mono tabular-nums text-foreground">
                  {data.imbalance}
                </span>
              </span>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="rounded-md border border-border p-4">
                <p className="text-sm text-muted-foreground">Total taken out (debits)</p>
                <p className="mt-1 font-mono text-xl font-semibold tabular-nums">
                  {data.total_debits}
                </p>
              </div>
              <div className="rounded-md border border-border p-4">
                <p className="text-sm text-muted-foreground">Total put in (credits)</p>
                <p className="mt-1 font-mono text-xl font-semibold tabular-nums">
                  {data.total_credits}
                </p>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              The two totals must match, so the difference should be 0.00. If it is not, stop and tell the engineering team. {CREDITS_LABEL}.
            </p>

            {data.accounts && data.accounts.length > 0 ? (
              <div className="overflow-hidden rounded-md border border-border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Account</TableHead>
                      <TableHead>Belongs to</TableHead>
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
        title="Billing and checks"
        description="Check that the books add up, refund a charge, approve a host payout, or close a dispute. Each action is saved in the audit log."
      />

      <div className="space-y-6">
        {!isAdmin ? (
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            <span className="text-muted-foreground">
              <span className="font-medium text-foreground">
                This page is for admins.
              </span>{" "}
              Your account does not look like an admin account. The actions below will be refused unless you have admin access.
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
