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
import { formatRelativeTime } from "@/lib/utils";
import { shortId } from "@/lib/jobs";

const amountSchema = z.object({
  amount: z
    .string()
    .trim()
    .refine((v) => /^\d+(\.\d{1,2})?$/.test(v), {
      message: "Enter a decimal like 25.00",
    })
    .refine((v) => Number(v) > 0, { message: "Amount must be greater than zero" }),
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
            Move available earnings out. {CREDITS_LABEL} — this is a simulated
            payout, not a real transfer.
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
                  — status {humanizeStatus(mutation.data.status)}. An operator
                  authorizes the settlement.
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
              <Label htmlFor="payout-amount">Amount</Label>
              <Input
                id="payout-amount"
                inputMode="decimal"
                placeholder="e.g. 25.00"
                aria-invalid={Boolean(errors.amount)}
                {...register("amount")}
              />
              {errors.amount ? (
                <p className="text-xs text-danger">{errors.amount.message}</p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  A payout larger than your available earnings is rejected.
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
        description="Request and track payouts of your available earnings. Amounts are simulated credits, shown verbatim."
        action={
          <div className="flex gap-2">
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
              <p className="text-sm text-muted-foreground">Available to pay out</p>
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
          <Card>
            <CardContent className="space-y-3 p-6">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </CardContent>
          </Card>
        ) : payoutsQuery.isError ? (
          <Card className="border-danger/40">
            <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
                <AlertCircle className="h-5 w-5" />
              </span>
              <h3 className="font-semibold">Couldn&apos;t load payouts</h3>
              <p className="max-w-md text-sm text-muted-foreground">
                {payoutsQuery.error instanceof ApiError
                  ? payoutsQuery.error.message
                  : "An unexpected error occurred."}
              </p>
              <Button variant="outline" onClick={() => payoutsQuery.refetch()}>
                <RefreshCw /> Try again
              </Button>
            </CardContent>
          </Card>
        ) : payouts.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
                <Banknote className="h-6 w-6" />
              </span>
              <h3 className="font-semibold">No payouts yet</h3>
              <p className="max-w-md text-sm text-muted-foreground">
                Request a payout of your available earnings and track its status
                here.
              </p>
              <Button onClick={() => setRequestOpen(true)}>
                <Plus /> Request payout
              </Button>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Payout</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Requested</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {payouts.map((p) => (
                  <TableRow key={p.payout_id}>
                    <TableCell className="font-mono text-xs">
                      {shortId(p.payout_id)}
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
                      {formatRelativeTime(p.created_at)}
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
