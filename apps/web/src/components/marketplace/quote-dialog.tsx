"use client";

import { parseApiDate } from "@/lib/format";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Loader2,
  LogIn,
  Receipt,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  ApiError,
  apiErrorCode,
  CREDITS_LABEL,
  type CreateQuoteInput,
  type MarketplaceMachine,
  type Quote,
  type Reservation,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const selectClass =
  "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-base text-foreground shadow-sm sm:text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

const schema = z.object({
  workload_type: z.string().min(1, "Choose a type of job."),
  estimated_minutes: z.coerce
    .number({ invalid_type_error: "Type a whole number of minutes, like 60." })
    .int("Use a whole number, like 60.")
    .min(1, "Use at least 1 minute.")
    .max(100000, "That is too many minutes. Use a smaller number."),
  gpu_count: z.coerce
    .number({ invalid_type_error: "Type a whole number, like 1." })
    .int("Use a whole number, like 1.")
    .min(1, "Use at least 1."),
  max_authorized_spend: z
    .string()
    .trim()
    .refine((v) => v === "" || /^\d+(\.\d{1,2})?$/.test(v), {
      message: "Type an amount like 5.00, or leave this empty.",
    }),
});

type FormValues = z.input<typeof schema>;

/** Human-readable messages for the known reserve/quote error codes. */
function reserveErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  switch (code) {
    case "no_capacity":
      return "This computer has no free spots right now. Pick another computer, or try again later.";
    case "quote_expired":
      return "This price quote has run out of time. Get a new quote to see a fresh price.";
    case "quote_already_reserved":
      return "You already reserved a spot with this quote. See it under Reservations.";
    case "budget_exceeded":
      return "The total is more than the most you said you would spend. Raise that limit or choose fewer minutes, then get a new quote.";
    default:
      if (err instanceof ApiError) return err.message;
      return "Something went wrong. Please try again in a minute.";
  }
}

function LineItem({
  label,
  value,
  strong,
}: {
  label: string;
  value: string;
  strong?: boolean;
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
      {/* Decimal strings are displayed verbatim — never parseFloat. */}
      <span className="font-mono tabular-nums">{value}</span>
    </div>
  );
}

/** Live countdown; returns remaining seconds (clamped at 0). */
function useCountdown(expiresAt: string | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!expiresAt) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [expiresAt]);
  if (!expiresAt) return 0;
  const target = parseApiDate(expiresAt).getTime();
  if (Number.isNaN(target)) return 0;
  return Math.max(0, Math.round((target - now) / 1000));
}

export function QuoteDialog({
  machine,
  open,
  onOpenChange,
}: {
  machine: MarketplaceMachine | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { accessToken, isAuthenticated } = useAuth();
  const [quote, setQuote] = useState<Quote | null>(null);
  const [reservation, setReservation] = useState<Reservation | null>(null);

  const workloads = machine?.supported_workloads ?? [];

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      workload_type: workloads[0] ?? "",
      estimated_minutes: 60,
      gpu_count: 1,
      max_authorized_spend: "",
    },
  });

  // Reset the whole flow whenever a different machine is opened.
  useEffect(() => {
    if (open && machine) {
      setQuote(null);
      setReservation(null);
      reset({
        workload_type: machine.supported_workloads[0] ?? "",
        estimated_minutes: 60,
        gpu_count: 1,
        max_authorized_spend: "",
      });
    }
  }, [open, machine, reset]);

  const quoteMut = useMutation({
    mutationFn: (input: CreateQuoteInput) =>
      api.createQuote(accessToken as string, input),
    onSuccess: (q) => setQuote(q),
  });

  const reserveMut = useMutation({
    mutationFn: (quoteId: string) =>
      api.reserveQuote(accessToken as string, quoteId),
    onSuccess: (r) => setReservation(r),
  });

  const secondsLeft = useCountdown(
    quote && !reservation ? quote.expires_at : null,
  );
  const expired = Boolean(quote) && !reservation && secondsLeft <= 0;

  const maxGpu = machine?.max_concurrent_jobs ?? machine?.gpu_count ?? 1;

  function onSubmit(values: FormValues) {
    if (!machine) return;
    const input: CreateQuoteInput = {
      machine_id: machine.machine_id,
      workload_type: values.workload_type,
      estimated_minutes: Number(values.estimated_minutes),
      gpu_count: Number(values.gpu_count),
    };
    const spend = String(values.max_authorized_spend).trim();
    if (spend) input.max_authorized_spend = spend;
    reserveMut.reset();
    quoteMut.mutate(input);
  }

  function getNewQuote() {
    setQuote(null);
    setReservation(null);
    quoteMut.reset();
    reserveMut.reset();
  }

  const countdownLabel = useMemo(() => {
    const m = Math.floor(secondsLeft / 60);
    const s = secondsLeft % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }, [secondsLeft]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {reservation
              ? "Spot reserved"
              : quote
                ? "Check your price quote"
                : "Get a price quote"}
          </DialogTitle>
          <DialogDescription>
            {machine ? (
              <>
                {machine.gpu_model ?? "CPU-only"}
                {machine.gpu_count > 1 ? ` ×${machine.gpu_count}` : ""}
                {machine.region ? ` · ${machine.region}` : ""} ·{" "}
                <span className="font-mono">{machine.price_per_hour}</span> per hour
              </>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        {/* --- Not authenticated: prompt to log in --- */}
        {!isAuthenticated ? (
          <div className="flex flex-col items-center gap-3 py-6 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-primary/10 text-primary">
              <LogIn className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Log in to get a price quote</h3>
            <p className="max-w-sm text-sm text-muted-foreground">
              Anyone can look at the list of computers. To get a price quote
              or reserve a spot, you need an account.
            </p>
            <div className="flex gap-2">
              <Button asChild variant="outline">
                <Link href="/login">Log in</Link>
              </Button>
              <Button asChild>
                <Link href="/register">Create an account</Link>
              </Button>
            </div>
          </div>
        ) : reservation ? (
          /* --- Reservation confirmation --- */
          <div className="space-y-4">
            <div className="flex items-start gap-3 rounded-md border border-success/40 bg-success/10 px-4 py-3 text-sm">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
              <div>
                <p className="font-medium text-foreground">
                  A spot is held for you on this computer.
                </p>
                <p className="mt-1 text-muted-foreground">
                  The spot is held until{" "}
                  {parseApiDate(reservation.expires_at).toLocaleString()}. No credits
                  are charged for holding a spot.
                </p>
              </div>
            </div>
            <dl className="space-y-2 rounded-md border border-border bg-surface-2/40 p-4 text-sm">
              <div className="flex justify-between">
                <dt className="text-muted-foreground">Reservation number</dt>
                <dd className="font-mono text-xs">
                  {reservation.reservation_id}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted-foreground">Status</dt>
                <dd>
                  <Badge variant="success">{reservation.status}</Badge>
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted-foreground">Spots held</dt>
                <dd className="font-mono">{reservation.slots}</dd>
              </div>
            </dl>
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Close
              </Button>
              <Button asChild>
                <Link href="/dashboard/reservations">See my reservations</Link>
              </Button>
            </DialogFooter>
          </div>
        ) : quote ? (
          /* --- Quote review + reserve --- */
          <div className="space-y-4">
            <div
              className={
                expired
                  ? "flex items-center justify-between rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm"
                  : "flex items-center justify-between rounded-md border border-border bg-surface-2/40 px-3 py-2 text-sm"
              }
            >
              <span className="flex items-center gap-2 text-muted-foreground">
                <Clock className="h-4 w-4" />
                {expired ? "Quote ran out of time" : "Price is held for"}
              </span>
              <span
                className={
                  expired
                    ? "font-mono font-semibold text-danger"
                    : "font-mono font-semibold tabular-nums"
                }
              >
                {expired ? "0:00" : countdownLabel}
              </span>
            </div>

            <div className="space-y-3 rounded-md border border-border p-4">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Receipt className="h-4 w-4 text-muted-foreground" /> What you would pay
              </div>
              <LineItem
                label="Price per hour"
                value={quote.breakdown.price_per_hour}
              />
              <LineItem
                label="Hours (your estimate)"
                value={quote.breakdown.estimated_hours}
              />
              <LineItem
                label={`Graphics card (GPU) cost (×${quote.gpu_count})`}
                value={quote.breakdown.gpu_cost}
              />
              <LineItem
                label={`Service fee (${quote.breakdown.platform_fee_rate})`}
                value={quote.breakdown.platform_fee}
              />
              <LineItem label="Total" value={quote.breakdown.total} strong />
              <p className="pt-1 text-xs text-muted-foreground">
                Amounts are in {CREDITS_LABEL}. The service fee is already
                included in the total.
              </p>
            </div>

            {quote.max_authorized_spend ? (
              <p className="text-xs text-muted-foreground">
                The most you said you would spend:{" "}
                <span className="font-mono">{quote.max_authorized_spend}</span>
              </p>
            ) : null}

            {reserveMut.isError ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{reserveErrorMessage(reserveMut.error)}</span>
              </div>
            ) : null}

            <DialogFooter>
              {expired ? (
                <Button onClick={getNewQuote}>Get a new quote</Button>
              ) : (
                <>
                  <Button variant="outline" onClick={getNewQuote}>
                    Get a new quote
                  </Button>
                  <Button
                    disabled={reserveMut.isPending}
                    onClick={() => reserveMut.mutate(quote.quote_id)}
                  >
                    {reserveMut.isPending ? (
                      <>
                        <Loader2 className="animate-spin" /> Reserving…
                      </>
                    ) : (
                      "Reserve a spot"
                    )}
                  </Button>
                </>
              )}
            </DialogFooter>
          </div>
        ) : (
          /* --- Quote form --- */
          <form
            onSubmit={handleSubmit(onSubmit)}
            noValidate
            className="space-y-4"
          >
            {quoteMut.isError ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{reserveErrorMessage(quoteMut.error)}</span>
              </div>
            ) : null}

            <div className="space-y-1.5">
              <Label htmlFor="workload_type">Type of job</Label>
              <select
                id="workload_type"
                className={selectClass}
                {...register("workload_type")}
              >
                {workloads.length === 0 ? (
                  <option value="">This computer has no job types yet</option>
                ) : (
                  workloads.map((w) => (
                    <option key={w} value={w}>
                      {w}
                    </option>
                  ))
                )}
              </select>
              {errors.workload_type ? (
                <p className="text-xs text-danger">
                  {errors.workload_type.message}
                </p>
              ) : null}
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="estimated_minutes">How many minutes do you need?</Label>
                <Input
                  id="estimated_minutes"
                  type="number"
                  min={1}
                  aria-invalid={Boolean(errors.estimated_minutes)}
                  {...register("estimated_minutes")}
                />
                {errors.estimated_minutes ? (
                  <p className="text-xs text-danger">
                    {errors.estimated_minutes.message}
                  </p>
                ) : null}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="gpu_count">Number of graphics cards (GPUs)</Label>
                <Input
                  id="gpu_count"
                  type="number"
                  min={1}
                  max={maxGpu}
                  aria-invalid={Boolean(errors.gpu_count)}
                  {...register("gpu_count")}
                />
                {errors.gpu_count ? (
                  <p className="text-xs text-danger">
                    {errors.gpu_count.message}
                  </p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    This computer has {machine?.gpu_count ?? 0}.
                  </p>
                )}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="max_authorized_spend">
                Most you want to spend (optional)
              </Label>
              <Input
                id="max_authorized_spend"
                inputMode="decimal"
                placeholder="For example: 5.00"
                aria-invalid={Boolean(errors.max_authorized_spend)}
                {...register("max_authorized_spend")}
              />
              {errors.max_authorized_spend ? (
                <p className="text-xs text-danger">
                  {errors.max_authorized_spend.message}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  If the total would be higher than this, we will not give you a quote. Amounts are in {CREDITS_LABEL}.
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
              <Button
                type="submit"
                disabled={quoteMut.isPending || workloads.length === 0}
              >
                {quoteMut.isPending ? (
                  <>
                    <Loader2 className="animate-spin" /> Getting your quote…
                  </>
                ) : (
                  "Get my price quote"
                )}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
