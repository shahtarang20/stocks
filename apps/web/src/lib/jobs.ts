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
  switch (status) {
    case "created":
      return "Just created";
    case "validating":
      return "Checking your job";
    case "queued":
      return "Waiting";
    case "matched":
      return "Computer found";
    case "reserved":
      return "Starting soon";
    case "running":
      return "Running";
    case "verifying":
      return "Checking the result";
    case "completed":
      return "Done";
    case "failed":
      return "Failed";
    case "timed_out":
      return "Ran out of time";
    case "cancelled":
      return "Cancelled";
    case "retry_pending":
      return "Trying again";
    case "disputed":
      return "Under review";
    default:
      return String(status).replace(/_/g, " ");
  }
}

/** One plain sentence: what this status means and, where it matters, what to do. */
export function jobStatusHelp(status: JobStatus): string {
  switch (status) {
    case "created":
    case "validating":
      return "We are checking your job before it starts. Nothing for you to do.";
    case "queued":
      return "Your job is waiting for a computer that fits it. Nothing for you to do. You can cancel it if you change your mind.";
    case "matched":
    case "reserved":
      return "A host's computer is picked and your credits are set aside. The job should start in a moment.";
    case "running":
      return "Your job is running on a host's computer now.";
    case "verifying":
      return "The job finished. We are checking the result.";
    case "completed":
      return "Your job finished. Open it to read the results and download files.";
    case "failed":
      return "Your job did not finish. You are not charged for a failed job. Read the log on the job page, fix the problem, and start it again.";
    case "timed_out":
      return "Your job took longer than the time limit you set, so it was stopped. Start it again with a longer time limit.";
    case "cancelled":
      return "This job was cancelled. You are not charged for a cancelled job.";
    case "retry_pending":
      return "The first computer could not finish. We are moving your job to another computer. Nothing for you to do.";
    case "disputed":
      return "This job has a dispute open. Our team is looking at it. Check the Disputes page for the answer.";
    default:
      return "";
  }
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
      return "This job would cost more than the most you said you would pay. Raise the most you will pay, or choose a cheaper option.";
    case "model_not_supported":
      return "That model cannot do this kind of job. Pick a different model, or try echo-sim-v1, the practice model.";
    case "invalid_input":
      return "The text you entered could not be used for this job. Check your prompt or list of items, then try again.";
    case "no_provider":
      return "No host has a free computer that fits your job right now. Try again in a few minutes, or ask for less (for example, fewer graphics cards).";
    case "machine_not_found":
      return "We could not find that computer. Check its ID, or leave the box empty and we will pick one for you.";
    case "gpu_count_exceeds_machine":
      return "That computer has fewer graphics cards (GPUs) than you asked for. Ask for fewer, or choose another computer.";
    case "isolation_tier_not_supported_by_machine":
      return "That computer does not offer the privacy level you picked. Choose \"Standard\", or pick a computer that supports it.";
    case "insufficient_credits":
      return "You do not have enough credits for this job. Add credits on the Credits and billing page, or ask for less.";
    case "spend_limit_exceeded":
      return "This job would go over a spending limit you set. Spending limits are set through the API (/spend-limits): raise or remove the limit there, or ask for less.";
    case "email_not_verified":
      return "Please confirm your email address before you run jobs. Use the button at the top of the page to get a new link.";
    default:
      if (err instanceof ApiError) {
        // Never show a raw snake_case API code to a user.
        return /^[a-z0-9]+(_[a-z0-9]+)+$/.test(err.message)
          ? err.message.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) + "."
          : err.message;
      }
      return "We could not submit your job. Check your internet connection and try again.";
  }
}

/**
 * Plain-words version of a job error code: what happened and what to do next.
 * The raw code stays visible on the page for support; this is the part to read first.
 */
export function jobErrorHelp(code: string | null | undefined): string {
  switch (code) {
    case "lease_expired":
      return "The host's computer stopped answering, so we stopped your job. Start it again. You are not charged for a job that fails.";
    case "thermal_shutdown":
      return "The host's graphics card got too hot, so the job was stopped to protect it. Start it again. It will usually go to a different computer.";
    case "gpu_unavailable":
    case "hardware_execution_unavailable":
      return "The host's computer could not run this job on real hardware. Start it again, or ask for fewer graphics cards. You are not charged.";
    case "container_nonzero_exit":
    case "executor_error":
      return "Your job started but stopped with an error. Read the log below for details, fix the problem and start it again.";
    case "missing_output":
      return "Your job ended without producing a result. Read the log below, check your input and start it again.";
    case "unsupported_workload":
      return "This kind of job is not supported yet. Choose a different type of job and try again.";
    case "vm_timeout":
      return "Your job ran past its time limit and was stopped. Start it again with a longer time limit.";
    case "egress_setup_failed":
      return "The host's computer could not set up the internet access your job asked for. Start it again, or turn off internet access for the job.";
    case "upload_failed":
    case "download_url_failed":
      return "We could not move your files in or out. Check your internet connection and start the job again.";
    case "abuse_suspected":
      return "Our safety checks stopped this job. If you think this is a mistake, open a dispute or contact support.";
    case "cancelled":
      return "This job was cancelled.";
    default:
      return "The job stopped with an error. Read the log below. If it is not clear, start the job again or contact support and give them the job ID.";
  }
}

/** Plain name for a kind of job. Unknown kinds are shown with spaces instead of dashes. */
export function jobTypeLabel(type: string | null | undefined): string {
  switch (type) {
    case "text-generation":
      return "Write text";
    case "batch-inference":
      return "Many prompts at once";
    case "embeddings":
      return "Text to numbers (embeddings)";
    case "kit":
      return "Kit (ready-made tool)";
    default:
      return type ? type.replace(/[-_]/g, " ").replace(/^./, (c) => c.toUpperCase()) : "Job";
  }
}
