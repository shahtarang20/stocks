"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CalendarClock,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  type Reservation,
  type ReservationStatus,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";

/** Derive the effective status — an active lease past its expiry is expired. */
function derivedStatus(r: Reservation): ReservationStatus {
  if (
    r.status === "active" &&
    r.expires_at &&
    new Date(r.expires_at).getTime() < Date.now()
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

  const [releaseTarget, setReleaseTarget] = useState<Reservation | null>(null);

  const query = useQuery<Reservation[]>({
    queryKey: ["reservations"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listReservations(token),
  });

  const releaseMut = useMutation({
    mutationFn: (id: string) => api.releaseReservation(token, id),
    onSuccess: () => {
      setReleaseTarget(null);
      queryClient.invalidateQueries({ queryKey: ["reservations"] });
    },
  });

  const reservations = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Reservations"
        description="Capacity slots you have reserved on marketplace machines, fetched live from GET /v1/reservations. Reservations hold a slot before job execution; no credits are charged here."
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
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4">
                <Skeleton className="h-10 w-10 rounded-md" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-1/3" />
                  <Skeleton className="h-3 w-1/2" />
                </div>
                <Skeleton className="h-6 w-16 rounded-full" />
              </div>
            ))}
          </CardContent>
        </Card>
      ) : query.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load reservations</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {query.error instanceof ApiError
                ? query.error.message
                : "An unexpected error occurred."}
            </p>
            <Button variant="outline" onClick={() => query.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : reservations.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
              <CalendarClock className="h-6 w-6" />
            </span>
            <h3 className="font-semibold">No reservations yet</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              Find a machine in the marketplace, request a quote, and reserve a
              slot. Your reservations will appear here.
            </p>
            <Button asChild>
              <a href="/marketplace/gpus">Browse the marketplace</a>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Machine</TableHead>
                <TableHead className="text-right">Slots</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead>Reserved</TableHead>
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
                      <div className="font-mono text-xs text-muted-foreground">
                        {r.machine_id}
                      </div>
                      <div className="font-mono text-[11px] text-muted-foreground/70">
                        quote {r.quote_id}
                      </div>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {r.slots}
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusVariant(status)}>{status}</Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatRelativeTime(r.expires_at)}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatRelativeTime(r.created_at)}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={!releasable}
                        className={releasable ? "text-danger" : undefined}
                        onClick={() => setReleaseTarget(r)}
                      >
                        Release
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
            <DialogTitle>Release this reservation?</DialogTitle>
            <DialogDescription>
              Releasing frees the held slot immediately so other customers can
              reserve it. This can&apos;t be undone — you&apos;d need a new quote
              to reserve again.
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
                  : "Couldn't release the reservation."}
              </span>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setReleaseTarget(null)}>
              Keep it
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
                  <Loader2 className="animate-spin" /> Releasing…
                </>
              ) : (
                "Release slot"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
