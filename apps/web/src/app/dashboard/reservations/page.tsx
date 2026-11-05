"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CalendarClock,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyId, EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import { capitalize, parseApiDate } from "@/lib/format";
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
  type Reservation,
  type ReservationStatus,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { HelpTip } from "@/components/ui/help-tip";

/** Derive the effective status — an active lease past its expiry is expired. */
function derivedStatus(r: Reservation): ReservationStatus {
  if (
    r.status === "active" &&
    r.expires_at &&
    parseApiDate(r.expires_at).getTime() < Date.now()
  ) {
    return "expired";
  }
  return r.status;
}

function statusVariant(status: ReservationStatus) {
  switch (status) {
    case "active":
      return "success" as const;
    case "consumed":
      return "default" as const;
    case "released":
      return "neutral" as const;
    case "expired":
      return "warning" as const;
  }
}

export default function ReservationsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();
  const toast = useToast();

  const [releaseTarget, setReleaseTarget] = useState<Reservation | null>(null);

  const query = useQuery<Reservation[]>({
    queryKey: ["reservations"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listReservations(token),
  });

  const releaseMut = useMutation({
    mutationFn: (id: string) => api.releaseReservation(token, id),
    onSuccess: () => {
      toast.success("Booking cancelled. The spot is free again.");
      setReleaseTarget(null);
      queryClient.invalidateQueries({ queryKey: ["reservations"] });
    },
  });

  const reservations = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Bookings"
        description="Time you have booked on a host's computer before running a job. A booking keeps a spot for you for a short time. You are not charged for a booking."
        action={
          <Button
            variant="outline"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {query.isLoading ? (
        <TableSkeleton rows={3} label="Loading reservations" />
      ) : query.isError ? (
        <ErrorState
          title="We could not load your bookings"
          message={query.error instanceof ApiError ? query.error.message : undefined}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : reservations.length === 0 ? (
        <EmptyState
          icon={CalendarClock}
          title="You have no bookings yet"
          action={
            <Button asChild>
              <Link href="/marketplace/gpus">Browse the marketplace</Link>
            </Button>
          }
        >
          To book time, open the marketplace, pick a computer, ask for a price, and accept it. Your
          bookings will appear here.
        </EmptyState>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Computer</TableHead>
                <TableHead className="text-right">
                  Slots
                  <HelpTip term="slots">
                    How many jobs you booked room for on this computer at the same time.
                  </HelpTip>
                </TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Ends</TableHead>
                <TableHead className="hidden sm:table-cell">Booked</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {reservations.map((r) => {
                const status = derivedStatus(r);
                const releasable = status === "active";
                return (
                  <TableRow key={r.reservation_id}>
                    <TableCell>
                      <CopyId id={r.machine_id} label="Copy machine id" className="text-muted-foreground" />
                      <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground/80">
                        price quote <CopyId id={r.quote_id} label="Copy quote id" />
                      </div>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {r.slots}
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusVariant(status)}>{capitalize(status)}</Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      <RelativeTime iso={r.expires_at} />
                    </TableCell>
                    <TableCell className="hidden text-sm text-muted-foreground sm:table-cell">
                      <RelativeTime iso={r.created_at} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={!releasable}
                        className={releasable ? "text-danger" : undefined}
                        onClick={() => setReleaseTarget(r)}
                      >
                        Cancel booking
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Card>
      )}

      <Dialog
        open={Boolean(releaseTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setReleaseTarget(null);
            releaseMut.reset();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel this booking?</DialogTitle>
            <DialogDescription>
              Cancelling gives the spot back right away, so other people can book it. You can&apos;t undo this. To book again, you need to ask for a new price.
            </DialogDescription>
          </DialogHeader>
          {releaseMut.isError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                {releaseMut.error instanceof ApiError
                  ? releaseMut.error.message
                  : "We could not cancel this booking. Please try again in a moment."}
              </span>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setReleaseTarget(null)}>
              Keep booking
            </Button>
            <Button
              variant="danger"
              disabled={releaseMut.isPending}
              onClick={() =>
                releaseTarget &&
                releaseMut.mutate(releaseTarget.reservation_id)
              }
            >
              {releaseMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Cancelling…
                </>
              ) : (
                "Cancel booking"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
