import { Badge } from "@/components/ui/badge";
import type { MachineStatus } from "@/lib/api";

const STATUS_VARIANT: Record<MachineStatus, "success" | "neutral" | "warning"> = {
  online: "success",
  offline: "neutral",
  maintenance: "warning",
};

/** Color-coded live operational status badge for a machine. */
export function MachineStatusBadge({ status }: { status: MachineStatus }) {
  return <Badge variant={STATUS_VARIANT[status] ?? "neutral"}>{status}</Badge>;
}
