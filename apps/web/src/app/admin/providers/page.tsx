"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, RefreshCw, Server } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";

export default function AdminProvidersPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery({
    queryKey: ["admin", "providers"],
    enabled: Boolean(accessToken),
    queryFn: () => api.searchMachines({}),
    refetchInterval: 20000,
  });

  const machines = query.data?.items ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Providers"
        description="All registered provider machines visible in the marketplace. Data is live from GET /v1/marketplace/machines."
        action={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="space-y-6">
        {query.isLoading ? (
          <Card>
            <CardContent className="space-y-3 p-6">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </CardContent>
          </Card>
        ) : query.isError ? (
          <Card className="border-danger/40">
            <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
              <AlertCircle className="h-8 w-8 text-danger" />
              <p className="text-sm text-muted-foreground">
                {query.error instanceof ApiError ? query.error.message : "Failed to load providers"}
              </p>
              <Button variant="outline" onClick={() => query.refetch()}>
                <RefreshCw /> Try again
              </Button>
            </CardContent>
          </Card>
        ) : machines.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
              <Server className="h-8 w-8 text-muted-foreground" />
              <h3 className="font-semibold">No machines online</h3>
              <p className="text-sm text-muted-foreground">
                No provider machines are currently online and priced in the marketplace.
              </p>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Machine ID</TableHead>
                  <TableHead>GPU</TableHead>
                  <TableHead>VRAM</TableHead>
                  <TableHead>Region</TableHead>
                  <TableHead>Price/hr</TableHead>
                  <TableHead>Verification</TableHead>
                  <TableHead>Last heartbeat</TableHead>
                  <TableHead>Slots</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {machines.map((m) => (
                  <TableRow key={m.machine_id}>
                    <TableCell className="font-mono text-xs text-primary">
                      {m.machine_id.slice(0, 8)}…
                    </TableCell>
                    <TableCell className="text-sm">{m.gpu_model ?? "—"}</TableCell>
                    <TableCell className="text-sm">
                      {m.vram_mb ? `${(m.vram_mb / 1024).toFixed(0)} GB` : "—"}
                    </TableCell>
                    <TableCell className="text-sm">{m.region ?? "—"}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {m.price_per_hour} {m.currency}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          m.verification_status === "verified"
                            ? "success"
                            : m.verification_status === "benchmarked"
                            ? "warning"
                            : "neutral"
                        }
                      >
                        {m.verification_status ?? "detected"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {m.last_heartbeat_at ? formatRelativeTime(m.last_heartbeat_at) : "—"}
                    </TableCell>
                    <TableCell className="text-sm">
                      {m.available_slots}/{m.max_concurrent_jobs}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </div>
    </>
  );
}
