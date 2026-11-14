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
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { HelpTip } from "@/components/ui/help-tip";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
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
import { formatMbAsGb } from "@/lib/utils";
import { capitalize } from "@/lib/format";

function hardwareSummary(m: Machine): string {
  const hw = m.hardware;
  const parts: string[] = [];
  if (hw.gpu_model) {
    const count = hw.gpu_count > 1 ? `${hw.gpu_count}× ` : "";
    parts.push(`${count}${hw.gpu_model}`);
  } else {
    parts.push("No graphics card (CPU only)");
  }
  return parts.join(" ");
}

export default function ProviderHardwarePage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();
  const toast = useToast();

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
      toast.success("Computer removed.");
      setDeregisterTarget(null);
      queryClient.invalidateQueries({ queryKey: ["provider", "machines"] });
    },
  });

  const machines = machinesQuery.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="My computers"
        description="The computers you have added. Check that each one is online, set its price, or remove it. What a computer says it has comes from the agent on that computer. ComputeCoin has not checked it yet."
        action={
          <div className="flex flex-wrap gap-2">
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
              <Link href="/provider/onboarding">Add my computer</Link>
            </Button>
          </div>
        }
      />

      {machinesQuery.isLoading ? (
        <TableSkeleton rows={3} label="Loading your computers" />
      ) : machinesQuery.isError ? (
        <ErrorState
          title="We could not load your computers"
          message={machinesQuery.error instanceof ApiError ? machinesQuery.error.message : undefined}
          onRetry={() => machinesQuery.refetch()}
          retrying={machinesQuery.isFetching}
        />
      ) : machines.length === 0 ? (
        <EmptyState
          icon={Cpu}
          title="You have not added a computer yet"
          action={
            <Button asChild>
              <Link href="/provider/onboarding">Start the setup guide</Link>
            </Button>
          }
        >
          Follow the setup guide to install the agent and connect your computer. It will show up here.
        </EmptyState>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Computer</TableHead>
                <TableHead className="hidden md:table-cell">Parts</TableHead>
                <TableHead className="hidden xl:table-cell">
                  What it reports
                  <HelpTip term="What it reports">
                    The agent on your computer lists its parts. ComputeCoin has not checked this list yet, so it is marked &quot;Self-reported&quot;.
                  </HelpTip>
                </TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden lg:table-cell">Last seen</TableHead>
                <TableHead className="text-right">Price per hour</TableHead>
                <TableHead className="hidden text-right lg:table-cell">Jobs at once</TableHead>
                <TableHead className="w-10"><span className="sr-only">Actions</span></TableHead>
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
                    {/* Hardware column is hidden on narrow screens, so repeat the summary here. */}
                    <div className="mt-0.5 text-xs md:hidden">{hardwareSummary(m)}</div>
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-sm font-medium">{hardwareSummary(m)}</span>
                      {m.hardware.gpu_vendor ? (
                        <Badge
                          variant="neutral"
                          className="text-[10px] uppercase font-mono px-1.5 py-0"
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
                        ? `${formatMbAsGb(m.hardware.vram_mb)} video memory · `
                        : ""}
                      {m.hardware.cpu_cores
                        ? `${m.hardware.cpu_cores} CPU cores · `
                        : ""}
                      {formatMbAsGb(m.hardware.ram_mb)} memory
                    </div>
                  </TableCell>
                  <TableCell className="hidden xl:table-cell">
                    <Badge variant="neutral" title="Reported by the agent on your computer. Not checked by ComputeCoin yet.">
                      {m.verification_status === "detected"
                        ? "Self-reported"
                        : capitalize(m.verification_status)}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <MachineStatusBadge status={m.status} />
                  </TableCell>
                  <TableCell className="hidden text-sm text-muted-foreground lg:table-cell">
                    <RelativeTime iso={m.last_heartbeat_at} empty="never" />
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {m.asking_price_per_hour ? `${m.asking_price_per_hour} credits` : "—"}
                  </TableCell>
                  <TableCell className="hidden text-right font-mono text-sm lg:table-cell">
                    {m.max_concurrent_jobs}
                  </TableCell>
                  <TableCell className="text-right">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Actions for ${m.hostname}`}
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
                          <Pencil className="h-4 w-4" /> Edit price and availability
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          className="text-danger focus:bg-danger/10"
                          onSelect={() => {
                            deregisterMut.reset();
                            setDeregisterTarget(m);
                          }}
                        >
                          <Trash2 className="h-4 w-4" /> Remove computer
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
            <DialogTitle>Remove this computer?</DialogTitle>
            <DialogDescription>
              {deregisterTarget?.hostname
                ? `"${deregisterTarget.hostname}" `
                : "This machine "}
              will be taken off ComputeCoin and its connection key will stop working.
              To add it again, create a new setup token and connect it from the beginning.
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
                  : "We could not remove the computer. Try again in a moment."}
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
                  <Loader2 className="animate-spin" /> Removing…
                </>
              ) : (
                "Remove computer"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
