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
import { CopyId, RelativeTime } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import { capitalize } from "@/lib/format";

const amountSchema = z.object({
  amount: z
    .string()
    .trim()
    .refine((v) => /^\d+(\.\d{1,2})?$/.test(v), {
      message: "Enter an amount like 50.00.",
    })
    .refine((v) => Number(v) > 0, { message: "Enter an amount greater than zero, like 50.00." }),
});

type AmountValues = z.infer<typeof amountSchema>;

const LEDGER_KIND_TEXT: Record<string, string> = {
  credit_purchase: "Credits added",
  hold: "Set aside for a job",
  capture: "Paid for a finished job",
  release: "Given back after a job",
  refund: "Refund",
  dispute_lost: "Dispute not in your favour",
};

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
  const toast = useToast();

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
    onSuccess: (data) => {
      toast.success(`Credits added. You now have ${data.available} to spend.`);
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
          <DialogTitle>Add credits</DialogTitle>
          <DialogDescription>
            Credits pay for your jobs. In this preview they are test credits: {CREDITS_LABEL}.
            No real money is charged and no card is used.
          </DialogDescription>
        </DialogHeader>

        {mutation.isSuccess ? (
          <div className="space-y-4">
            <div className="flex items-start gap-3 rounded-md border border-success/40 bg-success/10 px-4 py-3 text-sm">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
              <div>
                <p className="font-medium text-foreground">Credits added.</p>
                <p className="mt-1 text-muted-foreground">
                  You now have{" "}
                  <span className="font-mono">{mutation.data.available}</span>{" "}
                  {mutation.data.currency === "USDC_CREDITS" ? "test credits" : mutation.data.currency} to spend.
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
              <Label htmlFor="amount">How many credits to add</Label>
              <Input
                id="amount"
                inputMode="decimal"
                placeholder="e.g. 50.00"
                aria-invalid={Boolean(errors.amount)}
                aria-describedby="amount-help"
                autoComplete="off"
                {...register("amount")}
              />
              {errors.amount ? (
                <p id="amount-help" role="alert" className="text-xs text-danger">{errors.amount.message}</p>
              ) : (
                <p id="amount-help" className="text-xs text-muted-foreground">
                  Type a number, like 50.00. {CREDITS_LABEL}.
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
                  "Add credits"
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
        title="Credits and billing"
        description="Your credits and a list of everything that added or used them. In this preview the credits are test credits, not real money."
        action={
          <Button onClick={() => setBuyOpen(true)} disabled={!accessToken}>
            <Plus /> Add credits
          </Button>
        }
      />

      <div className="space-y-6">
        {/* ---- Balance ---- */}
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
            <CardTitle className="flex items-center gap-2">
              <Wallet className="h-4 w-4 text-muted-foreground" /> Your credits
            </CardTitle>
            <Badge variant="neutral">{CREDITS_LABEL}</Badge>
          </CardHeader>
          <CardContent>
            {accountQuery.isLoading ? (
              <div className="grid grid-cols-2 gap-6 sm:grid-cols-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="space-y-2">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-8 w-28" />
                  </div>
                ))}
              </div>
            ) : accountQuery.isError || !account ? (
              <div role="alert" className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  {accountQuery.error instanceof ApiError
                    ? accountQuery.error.message
                    : "We could not load your credits. Press Retry."}
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
                <div className="grid grid-cols-2 gap-6 sm:grid-cols-3">
                  <div className="col-span-2 sm:col-span-1">
                    <BalanceStat
                      label="Ready to spend"
                      value={account.available}
                      emphasis
                    />
                  </div>
                  <BalanceStat label="Held for running jobs" value={account.reserved} />
                  <BalanceStat label="Total" value={account.balance} />
                </div>
                <p className="mt-4 text-xs text-muted-foreground">
                  Ready to spend is what you can use now. Held credits are set aside while a
                  job runs, and any left over are given back when it ends. Total is the two added together.
                  These are test credits, not real money.
                </p>
              </>
            )}
          </CardContent>
        </Card>

        {/* ---- Ledger ---- */}
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
            <CardTitle>History</CardTitle>
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
                <h3 className="font-semibold">We could not load your history</h3>
                <p className="max-w-md text-sm text-muted-foreground">
                  {ledgerQuery.error instanceof ApiError
                    ? ledgerQuery.error.message
                    : "Something went wrong on our side. Try again in a moment."}
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
                <h3 className="font-semibold">Nothing here yet</h3>
                <p className="max-w-md text-sm text-muted-foreground">
                  Each time you add credits, a job sets some aside, a job is paid for, or you get a refund,
                  it is listed here. Add credits to get started.
                </p>
                <Button onClick={() => setBuyOpen(true)}>
                  <Plus /> Add credits
                </Button>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>What happened</TableHead>
                    <TableHead>Added or taken</TableHead>
                    <TableHead className="text-right">Credits</TableHead>
                    <TableHead className="hidden md:table-cell">Job or order</TableHead>
                    <TableHead>Date</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {ledger.map((e) => (
                    <TableRow key={e.entry_id}>
                      <TableCell className="text-sm">
                        {LEDGER_KIND_TEXT[e.kind] ?? humanizeStatus(e.kind)}
                      </TableCell>
                      <TableCell>
                        <Badge variant={ledgerDirectionVariant(e.direction)}>
                          {e.direction === "credit" ? "Added" : e.direction === "debit" ? "Taken" : capitalize(e.direction)}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {e.direction === "credit" ? "+" : "−"}
                        {e.amount}
                      </TableCell>
                      <TableCell className="hidden text-muted-foreground md:table-cell">
                        {e.reference_id ?? e.reference ? <CopyId id={(e.reference_id ?? e.reference) as string} length={12} label="Copy reference" /> : "—"}
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

      <BuyCreditsDialog open={buyOpen} onOpenChange={setBuyOpen} />
    </>
  );
}
