"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Cpu,
  Loader2,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { ConsolePageHeader } from "@/components/console/console-page";
import { EditMachineDialog } from "@/components/provider/edit-machine-dialog";
import { MachineStatusBadge } from "@/components/provider/machine-status-badge";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError, type Machine } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatMbAsGb, formatRelativeTime, formatUsd } from "@/lib/utils";

function hardwareSummary(m: Machine): string {
  const hw = m.hardware;
  const parts: string[] = [];
  if (hw.gpu_model) {
    const count = hw.gpu_count > 1 ? `${hw.gpu_count}× ` : "";
    parts.push(`${count}${hw.gpu_model}`);
  } else {
    parts.push("CPU-only");
  }
  return parts.join(" ");
}

export default function ProviderHardwarePage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();

  const [editTarget, setEditTarget] = useState<Machine | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [deregisterTarget, setDeregisterTarget] = useState<Machine | null>(null);

  const machinesQuery = useQuery<Machine[]>({
    queryKey: ["provider", "machines"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listMachines(token),
  });

  const deregisterMut = useMutation({
    mutationFn: (id: string) => api.deregisterMachine(token, id),
    onSuccess: () => {
      setDeregisterTarget(null);
      queryClient.invalidateQueries({ queryKey: ["provider", "machines"] });
    },
  });

  const machines = machinesQuery.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Hardware"
        description="Machines you have enrolled, fetched live from GET /provider/machines. Capability is self-reported by the agent (Detected)."
        action={
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => machinesQuery.refetch()}
              disabled={machinesQuery.isFetching}
            >
              <RefreshCw
                className={machinesQuery.isFetching ? "animate-spin" : undefined}
              />
              Refresh
            </Button>
            <Button asChild>
              <Link href="/provider/onboarding">Add machine</Link>
            </Button>
          </div>
        }
      />

      {machinesQuery.isLoading ? (
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
      ) : machinesQuery.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load machines</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {machinesQuery.error instanceof ApiError
                ? machinesQuery.error.message
                : "An unexpected error occurred."}
            </p>
            <Button variant="outline" onClick={() => machinesQuery.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : machines.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
              <Cpu className="h-6 w-6" />
            </span>
            <h3 className="font-semibold">No machines yet</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              Install the agent and enroll a machine to see it here.
            </p>
            <Button asChild>
              <Link href="/provider/onboarding">Install the agent</Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Machine</TableHead>
                <TableHead>Hardware</TableHead>
                <TableHead>Capability</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Last heartbeat</TableHead>
                <TableHead className="text-right">Price / hr</TableHead>
                <TableHead className="text-right">Max jobs</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {machines.map((m) => (
                <TableRow key={m.machine_id}>
                  <TableCell>
                    <div className="font-medium">{m.hostname}</div>
                    <div className="font-mono text-xs text-muted-foreground">
                      {m.region ?? "—"}
                      {m.agent_version ? ` · v${m.agent_version}` : ""}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-sm font-medium">{hardwareSummary(m)}</span>
                      {m.hardware.gpu_vendor ? (
                        <Badge
                          variant="neutral"
                          className={
                            m.hardware.gpu_vendor === "amd"
                              ? "border-red-500/30 text-red-400 bg-red-500/10 text-[10px] uppercase font-mono px-1.5 py-0"
                              : "border-emerald-500/30 text-emerald-400 bg-emerald-500/10 text-[10px] uppercase font-mono px-1.5 py-0"
                          }
                        >
                          {m.hardware.gpu_vendor}
                        </Badge>
                      ) : null}
                      {m.hardware.gpu_runtime ? (
                        <Badge
                          variant="neutral"
                          className="border-primary/30 text-primary bg-primary/10 text-[10px] uppercase font-mono px-1.5 py-0"
                        >
                          {m.hardware.gpu_runtime}
                        </Badge>
                      ) : null}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {m.hardware.gpu_model
                        ? `${formatMbAsGb(m.hardware.vram_mb)} VRAM · `
                        : ""}
                      {m.hardware.cpu_cores
                        ? `${m.hardware.cpu_cores} vCPU · `
                        : ""}
                      {formatMbAsGb(m.hardware.ram_mb)} RAM
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="neutral" title="Self-reported by the agent">
                      {m.verification_status === "detected"
                        ? "Detected"
                        : m.verification_status}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <MachineStatusBadge status={m.status} />
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatRelativeTime(m.last_heartbeat_at)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {m.asking_price_per_hour
                      ? formatUsd(Number(m.asking_price_per_hour))
                      : "—"}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {m.max_concurrent_jobs}
                  </TableCell>
                  <TableCell className="text-right">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label="Machine actions"
                        >
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          onSelect={() => {
                            setEditTarget(m);
                            setEditOpen(true);
                          }}
                        >
                          <Pencil className="h-4 w-4" /> Edit
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          className="text-danger focus:bg-danger/10"
                          onSelect={() => setDeregisterTarget(m)}
                        >
                          <Trash2 className="h-4 w-4" /> Deregister
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      <EditMachineDialog
        machine={editTarget}
        open={editOpen}
        onOpenChange={setEditOpen}
      />

      <Dialog
        open={Boolean(deregisterTarget)}
        onOpenChange={(open) => {
          if (!open) setDeregisterTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Deregister machine?</DialogTitle>
            <DialogDescription>
              {deregisterTarget?.hostname
                ? `"${deregisterTarget.hostname}" `
                : "This machine "}
              will be removed from the network and its agent credential revoked.
              You&apos;ll need to re-enroll with a new token to bring it back.
            </DialogDescription>
          </DialogHeader>
          {deregisterMut.isError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                {deregisterMut.error instanceof ApiError
                  ? deregisterMut.error.message
                  : "Couldn't deregister the machine."}
              </span>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeregisterTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              disabled={deregisterMut.isPending}
              onClick={() =>
                deregisterTarget &&
                deregisterMut.mutate(deregisterTarget.machine_id)
              }
            >
              {deregisterMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Deregistering…
                </>
              ) : (
                "Deregister"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
