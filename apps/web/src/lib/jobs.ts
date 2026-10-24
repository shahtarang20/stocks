/**
 * Shared presentation helpers for Phase 4 jobs.
 *
 * HONESTY: execution_mode "simulated" is a deterministic transform, never real
 * model inference — it must be surfaced wherever output is shown. Cost is never
 * charged in Phase 4 (final_cost is null); billing lands in Phase 5.
 */

import {
  apiErrorCode,
  ApiError,
  isTerminalJobStatus,
  type JobExecutionMode,
  type JobStatus,
  type LogLine,
} from "@/lib/api";

export type BadgeVariant =
  | "default"
  | "neutral"
  | "success"
  | "warning"
  | "danger"
  | "outline";

/** Color-coded badge variant for an FSM status. */
export function jobStatusVariant(status: JobStatus): BadgeVariant {
  switch (status) {
    case "completed":
      return "success";
    case "failed":
    case "timed_out":
    case "disputed":
      return "danger";
    case "cancelled":
      return "neutral";
    case "queued":
    case "matched":
    case "reserved":
    case "running":
    case "validating":
    case "created":
    case "retry_pending":
      return "default";
    default:
      return "neutral";
  }
}

/** Human label for an FSM status (keeps the lowercase contract value honest). */
export function jobStatusLabel(status: JobStatus): string {
  return status.replace(/_/g, " ");
}

/**
 * "Active" = non-terminal, so polling should continue. Terminal states
 * (completed/failed/timed_out/cancelled/disputed) stop polling.
 */
export function isJobActive(status: JobStatus): boolean {
  return !isTerminalJobStatus(status);
}

/** A short, copy-free display id (first segment of a UUID). */
export function shortId(id: string | undefined | null): string {
  if (!id) return "—";
  return id.length > 8 ? id.slice(0, 8) : id;
}

export function isSimulated(mode: JobExecutionMode | undefined): boolean {
  return mode === "simulated";
}

/** Normalize a single log line (bare string or structured row) to text. */
export function logLineText(line: LogLine): string {
  if (typeof line === "string") return line;
  const ts = line.ts ?? line.timestamp;
  const level = line.level;
  const msg = line.line ?? line.message ?? JSON.stringify(line);
  return [ts, level ? level.toUpperCase() : null, msg]
    .filter(Boolean)
    .join(" ");
}

/** Map known create-job error codes to clear, honest messages. */
export function createJobErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  switch (code) {
    case "budget_exceeded":
      return "The estimated cost exceeds your max cost. Raise the budget cap or pick a cheaper target.";
    case "model_not_supported":
      return "The selected model does not support this workload type. Try echo-sim-v1, the approved simulated model.";
    case "invalid_input":
      return "The job input is not valid for this workload. Check the prompt / items payload and try again.";
    case "no_provider":
      return "No eligible provider is available right now. Try again shortly or widen your requirements.";
    case "machine_not_found":
      return "That machine was not found. Check the machine ID, or leave it blank to let the scheduler choose.";
    case "gpu_count_exceeds_machine":
      return "That machine has fewer GPUs than you requested. Lower the GPU count or choose another machine.";
    case "isolation_tier_not_supported_by_machine":
      return "That machine does not offer the selected isolation tier. Choose \"container\" or a machine that supports it.";
    case "insufficient_credits":
      return "You don't have enough credits for this job. Buy credits on the Billing page, or lower the estimate.";
    case "spend_limit_exceeded":
      return "This job would exceed one of your spend limits.";
    case "email_not_verified":
      return "Verify your email address before submitting jobs. Use the banner at the top of the page to resend the link.";
    default:
      if (err instanceof ApiError) {
        // Never show a raw snake_case API code to a user.
        return /^[a-z0-9]+(_[a-z0-9]+)+$/.test(err.message)
          ? err.message.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) + "."
          : err.message;
      }
      return "Something went wrong submitting the job. Please try again.";
  }
}
