import { Badge } from "@/components/ui/badge";
import { capitalize } from "@/lib/format";
import { isSimulated, jobStatusHelp, jobStatusLabel, jobStatusVariant } from "@/lib/jobs";
import type { JobExecutionMode, JobStatus } from "@/lib/api";

/** Job status: colour AND a text label (never colour alone). */
export function JobStatusBadge({ status }: { status: JobStatus }) {
  const variant = jobStatusVariant(status);
  // `bg-success/12` in the shared Badge is not a valid Tailwind opacity step, so the tint is
  // missing there; set a valid one here so success and danger badges match warning.
  const tint = variant === "success" ? "bg-success/10" : variant === "danger" ? "bg-danger/10" : undefined;
  return (
    <Badge variant={variant} className={tint} title={jobStatusHelp(status)}>
      {capitalize(jobStatusLabel(status))}
    </Badge>
  );
}

/** Execution mode. Practice mode (simulated) must stay clearly visible: it is NOT real inference. */
export function ExecutionModeBadge({ mode }: { mode: JobExecutionMode | undefined }) {
  const simulated = isSimulated(mode);
  return (
    <Badge
      variant={simulated ? "warning" : "neutral"}
      title={simulated ? "Practice mode: the result is a simple placeholder, not real AI output." : "Ran on a host's computer."}
    >
      {simulated ? "Practice mode" : "Real computer"}
    </Badge>
  );
}
