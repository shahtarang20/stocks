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
  Banknote,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { CopyId, EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
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
  type Payout,
  type ProviderEarnings,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { billingErrorMessage, payoutStatusVariant, humanizeStatus } from "@/lib/billing";

const amountSchema = z.object({
  amount: z
    .string()
    .trim()
    .refine((v) => /^\d+(\.\d{1,2})?$/.test(v), {
      message: "Enter an amount like 25.00.",
    })
    .refine((v) => Number(v) > 0, { message: "The amount must be more than zero." }),
});

type AmountValues = z.infer<typeof amountSchema>;

function RequestPayoutDialog({
  open,
  onOpenChange,
  available,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  available: string | undefined;
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

  const mutation = useMutation<Payout, unknown, AmountValues>({
    mutationFn: (values) =>
      api.requestPayout(accessToken as string, { amount: values.amount }),
    onSuccess: () => {
      toast.success("Payout requested. A person will review it.");
      queryClient.invalidateQueries({ queryKey: ["payouts"] });
      queryClient.invalidateQueries({ queryKey: ["provider-earnings"] });
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
          <DialogTitle>Request a payout</DialogTitle>
          <DialogDescription>
            Ask to take some of your earnings out. {CREDITS_LABEL}. This is a practice
            payout. No real money moves.
            {available ? (
              <>
                {" "}
                Available:{" "}
                <span className="font-mono">{available}</span>.
              </>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        {mutation.isSuccess ? (
          <div className="space-y-4">
            <div className="flex items-start gap-3 rounded-md border border-success/40 bg-success/10 px-4 py-3 text-sm">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
              <div>
                <p className="font-medium text-foreground">Payout requested.</p>
                <p className="mt-1 text-muted-foreground">
                  Amount <span className="font-mono">{mutation.data.amount}</span>{" "}
                  — status {humanizeStatus(mutation.data.status)}. An administrator
                  needs to approve it. Check the Payouts page for updates.
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
              <Label htmlFor="payout-amount">How much do you want to take out?</Label>
              <Input
                id="payout-amount"
                inputMode="decimal"
                placeholder="e.g. 25.00"
                aria-invalid={Boolean(errors.amount)}
                aria-describedby="payout-amount-help"
                autoComplete="off"
                {...register("amount")}
              />
              {errors.amount ? (
                <p id="payout-amount-help" role="alert" className="text-xs text-danger">{errors.amount.message}</p>
              ) : (
                <p id="payout-amount-help" className="text-xs text-muted-foreground">
                  You cannot ask for more than you have earned. Use numbers like 25.00.
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
                    <Loader2 className="animate-spin" /> Requesting…
                  </>
                ) : (
                  "Request payout"
                )}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function ProviderPayoutsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const [requestOpen, setRequestOpen] = useState(false);

  const earningsQuery = useQuery<ProviderEarnings>({
    queryKey: ["provider-earnings"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getProviderEarnings(token),
  });

  const payoutsQuery = useQuery<Payout[]>({
    queryKey: ["payouts"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listPayouts(token),
  });

  const payouts = payoutsQuery.data ?? [];
  const available = earningsQuery.data?.available;

  return (
    <>
      <ConsolePageHeader
        title="Payouts"
        description="Ask to take out your earnings and follow each request. You ask, an administrator approves. Amounts are test credits. Real bank payouts are not switched on yet."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => payoutsQuery.refetch()}
              disabled={payoutsQuery.isFetching || !accessToken}
            >
              <RefreshCw
                className={payoutsQuery.isFetching ? "animate-spin" : undefined}
              />
              Refresh
            </Button>
            <Button onClick={() => setRequestOpen(true)} disabled={!accessToken}>
              <Plus /> Request payout
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        <Card>
          <CardContent className="flex flex-wrap items-center justify-between gap-4 p-5">
            <div>
              <p className="text-sm text-muted-foreground">Ready to pay out</p>
              {earningsQuery.isLoading ? (
                <Skeleton className="mt-1 h-8 w-28" />
              ) : (
                <p className="mt-1 font-mono text-2xl font-bold tabular-nums">
                  {available ?? "0.00"}
                </p>
              )}
            </div>
            <Badge variant="neutral">{CREDITS_LABEL}</Badge>
          </CardContent>
        </Card>

        {payoutsQuery.isLoading ? (
          <TableSkeleton rows={3} label="Loading payouts" />
        ) : payoutsQuery.isError ? (
          <ErrorState
            title="We could not load your payouts"
            message={payoutsQuery.error instanceof ApiError ? payoutsQuery.error.message : undefined}
            onRetry={() => payoutsQuery.refetch()}
            retrying={payoutsQuery.isFetching}
          />
        ) : payouts.length === 0 ? (
          <EmptyState
            icon={Banknote}
            title="No payouts yet"
            action={
              <Button onClick={() => setRequestOpen(true)}>
                <Plus /> Request payout
              </Button>
            }
          >
            You have not asked for a payout yet. When you have earnings, press Request payout. You can follow its status here.
          </EmptyState>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Request</TableHead>
                  <TableHead className="text-right">Amount (credits)</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Requested</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {payouts.map((p) => (
                  <TableRow key={p.payout_id}>
                    <TableCell>
                      <CopyId id={p.payout_id} label="Copy payout id" />
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums font-medium">
                      {p.amount}
                    </TableCell>
                    <TableCell>
                      <Badge variant={payoutStatusVariant(p.status)}>
                        {humanizeStatus(p.status)}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      <RelativeTime iso={p.created_at} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </div>

      <RequestPayoutDialog
        open={requestOpen}
        onOpenChange={setRequestOpen}
        available={available}
      />
    </>
  );
}
