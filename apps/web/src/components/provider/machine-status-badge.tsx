import { Badge } from "@/components/ui/badge";
import type { MachineStatus } from "@/lib/api";

const STATUS_VARIANT: Record<MachineStatus, "success" | "neutral" | "warning"> = {
  online: "success",
  offline: "neutral",
  maintenance: "warning",
};

const STATUS_LABEL: Record<MachineStatus, string> = {
  online: "Online",
  offline: "Offline",
  maintenance: "Paused (maintenance)",
};

const STATUS_HELP: Record<MachineStatus, string> = {
  online: "ComputeCoin heard from this computer in the last 90 seconds.",
  offline: "ComputeCoin has not heard from this computer recently. Check that the agent is running.",
  maintenance: "You paused this computer. It will not get new jobs.",
};

/** Color-coded live status badge for a computer, in plain words. */
export function MachineStatusBadge({ status }: { status: MachineStatus }) {
  return (
    <Badge variant={STATUS_VARIANT[status] ?? "neutral"} title={STATUS_HELP[status]}>
      {STATUS_LABEL[status] ?? status.charAt(0).toUpperCase() + status.slice(1)}
    </Badge>
  );
}
