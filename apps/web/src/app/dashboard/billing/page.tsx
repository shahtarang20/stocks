"use client";

import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Plus,
  RefreshCw,
  Wallet,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
  type BillingAccount,
  type LedgerEntry,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { billingErrorMessage, ledgerDirectionVariant, humanizeStatus } from "@/lib/billing";
import { formatRelativeTime } from "@/lib/utils";

const amountSchema = z.object({
  amount: z
    .string()
    .trim()
    .refine((v) => /^\d+(\.\d{1,2})?$/.test(v), {
      message: "Enter a decimal like 50.00",
    })
    .refine((v) => Number(v) > 0, { message: "Amount must be greater than zero" }),
});

type AmountValues = z.infer<typeof amountSchema>;

function BalanceStat({
  label,
  value,
  emphasis,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
}) {
  return (
    <div>
      <p className="text-sm text-muted-foreground">{label}</p>
      {/* Decimal strings are shown verbatim — never parseFloat. */}
      <p
        className={
          emphasis
            ? "mt-1 font-mono text-3xl font-bold tabular-nums"
            : "mt-1 font-mono text-2xl font-semibold tabular-nums"
        }
      >
        {value}
      </p>
    </div>
  );
}

function BuyCreditsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<AmountValues>({
    resolver: zodResolver(amountSchema),
    defaultValues: { amount: "" },
  });

  const mutation = useMutation<BillingAccount, unknown, AmountValues>({
    mutationFn: (values) =>
      api.purchaseCredits(accessToken as string, { amount: values.amount }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["billing-account"] });
      queryClient.invalidateQueries({ queryKey: ["billing-ledger"] });
    },
  });

  useEffect(() => {
    if (open) {
      reset({ amount: "" });
      mutation.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Buy credits</DialogTitle>
          <DialogDescription>
            Top up your simulated balance. {CREDITS_LABEL} — no real money changes
            hands and no real payment rail is used.
          </DialogDescription>
        </DialogHeader>

        {mutation.isSuccess ? (
          <div className="space-y-4">
            <div className="flex items-start gap-3 rounded-md border border-success/40 bg-success/10 px-4 py-3 text-sm">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
              <div>
                <p className="font-medium text-foreground">Credits added.</p>
                <p className="mt-1 text-muted-foreground">
                  New available balance:{" "}
                  <span className="font-mono">{mutation.data.available}</span>{" "}
                  {mutation.data.currency}.
                </p>
              </div>
            </div>
            <DialogFooter>
              <Button onClick={() => onOpenChange(false)}>Done</Button>
            </DialogFooter>
          </div>
        ) : (
          <form
            onSubmit={handleSubmit((v) => mutation.mutate(v))}
            noValidate
            className="space-y-4"
          >
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
              <Label htmlFor="amount">Amount (credits)</Label>
              <Input
                id="amount"
                inputMode="decimal"
                placeholder="e.g. 50.00"
                aria-invalid={Boolean(errors.amount)}
                {...register("amount")}
              />
              {errors.amount ? (
                <p className="text-xs text-danger">{errors.amount.message}</p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Simulated top-up. {CREDITS_LABEL}.
                </p>
              )}
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? (
                  <>
                    <Loader2 className="animate-spin" /> Adding…
                  </>
                ) : (
                  "Buy credits"
                )}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function BillingPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const [buyOpen, setBuyOpen] = useState(false);

  const accountQuery = useQuery<BillingAccount>({
    queryKey: ["billing-account"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getBillingAccount(token),
  });

  const ledgerQuery = useQuery<LedgerEntry[]>({
    queryKey: ["billing-ledger"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getLedger(token),
  });

  const account = accountQuery.data;
  const ledger = ledgerQuery.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Billing"
        description="Live balance and double-entry ledger from the billing service. Amounts are simulated credits — shown verbatim, never converted to fiat."
        action={
          <Button onClick={() => setBuyOpen(true)} disabled={!accessToken}>
            <Plus /> Buy credits
          </Button>
        }
      />

      <div className="space-y-6">
        {/* ---- Balance ---- */}
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
            <CardTitle className="flex items-center gap-2">
              <Wallet className="h-4 w-4 text-muted-foreground" /> Balance
            </CardTitle>
            <Badge variant="neutral">{CREDITS_LABEL}</Badge>
          </CardHeader>
          <CardContent>
            {accountQuery.isLoading ? (
              <div className="grid gap-6 sm:grid-cols-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="space-y-2">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-8 w-28" />
                  </div>
                ))}
              </div>
            ) : accountQuery.isError || !account ? (
              <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  {accountQuery.error instanceof ApiError
                    ? accountQuery.error.message
                    : "Couldn't load your balance."}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="ml-2"
                    onClick={() => accountQuery.refetch()}
                  >
                    <RefreshCw /> Retry
                  </Button>
                </span>
              </div>
            ) : (
              <>
                <div className="grid gap-6 sm:grid-cols-3">
                  <BalanceStat
                    label="Available (spendable)"
                    value={account.available}
                    emphasis
                  />
                  <BalanceStat label="Reserved (holds)" value={account.reserved} />
                  <BalanceStat label="Total owned" value={account.balance} />
                </div>
                <p className="mt-4 text-xs text-muted-foreground">
                  {account.currency} · available = spendable, reserved = active
                  holds on in-flight jobs, total = available + reserved. These are
                  simulated credits, not real currency.
                </p>
              </>
            )}
          </CardContent>
        </Card>

        {/* ---- Ledger ---- */}
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
            <CardTitle>Ledger</CardTitle>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => ledgerQuery.refetch()}
              disabled={ledgerQuery.isFetching}
            >
              <RefreshCw className={ledgerQuery.isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
          </CardHeader>
          <CardContent className="p-0">
            {ledgerQuery.isLoading ? (
              <div className="space-y-3 p-6">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-10 w-full" />
                ))}
              </div>
            ) : ledgerQuery.isError ? (
              <div className="flex flex-col items-center gap-3 p-10 text-center">
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
                  <AlertCircle className="h-5 w-5" />
                </span>
                <h3 className="font-semibold">Couldn&apos;t load the ledger</h3>
                <p className="max-w-md text-sm text-muted-foreground">
                  {ledgerQuery.error instanceof ApiError
                    ? ledgerQuery.error.message
                    : "An unexpected error occurred."}
                </p>
                <Button variant="outline" onClick={() => ledgerQuery.refetch()}>
                  <RefreshCw /> Try again
                </Button>
              </div>
            ) : ledger.length === 0 ? (
              <div className="flex flex-col items-center gap-3 p-12 text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
                  <Wallet className="h-6 w-6" />
                </span>
                <h3 className="font-semibold">No ledger entries yet</h3>
                <p className="max-w-md text-sm text-muted-foreground">
                  Every credit purchase, hold, capture, release and refund is
                  recorded here as an immutable double-entry row.
                </p>
                <Button onClick={() => setBuyOpen(true)}>
                  <Plus /> Buy credits
                </Button>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Kind</TableHead>
                    <TableHead>Direction</TableHead>
                    <TableHead className="text-right">Amount</TableHead>
                    <TableHead>Reference</TableHead>
                    <TableHead>Date</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {ledger.map((e) => (
                    <TableRow key={e.entry_id}>
                      <TableCell className="text-sm">
                        {humanizeStatus(e.kind)}
                      </TableCell>
                      <TableCell>
                        <Badge variant={ledgerDirectionVariant(e.direction)}>
                          {e.direction}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {e.direction === "credit" ? "+" : "−"}
                        {e.amount}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">
                        {e.reference ?? "—"}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {formatRelativeTime(e.created_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>

      <BuyCreditsDialog open={buyOpen} onOpenChange={setBuyOpen} />
    </>
  );
}
