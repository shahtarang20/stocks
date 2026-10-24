/**
 * Phase 5 billing presentation helpers.
 *
 * HONESTY: all money is a decimal STRING displayed verbatim — never parseFloat.
 * Currency "USDC_CREDITS" is SIMULATED test credits, always labelled in the UI.
 * Estimated vs actual cost are tracked and shown separately. A failed/cancelled
 * job is never charged.
 */

import { ApiError, apiErrorCode } from "@/lib/api";
import type { BadgeVariant } from "@/lib/jobs";

/** Human-readable message for known billing error codes. */
export function billingErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  switch (code) {
    case "insufficient_credits":
      return "Not enough credits for this amount. Buy more simulated credits and try again.";
    case "insufficient_earnings":
      return "The requested payout exceeds your available earnings.";
    case "no_charge_to_refund":
      return "There is no settled charge to refund for this job or invoice.";
    case "invalid_amount":
      return "Enter a valid decimal amount greater than zero.";
    case "job_not_settled":
      return "This job has not settled yet — it can only be disputed after it completes.";
    case "dispute_exists":
      return "A dispute is already open for this job.";
    case "email_not_verified":
      return "Verify your email address first. Use the banner at the top of the page to resend the link.";
    default:
      if (err instanceof ApiError && err.message) {
        // Never show a raw snake_case API code to a user.
        return /^[a-z0-9]+(_[a-z0-9]+)+$/.test(err.message)
          ? err.message.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) + "."
          : err.message;
      }
      return "Something went wrong. Please try again.";
  }
}

/** Badge color for a ledger entry direction. */
export function ledgerDirectionVariant(direction: string): BadgeVariant {
  return direction === "credit" ? "success" : "neutral";
}

/** Badge color for an invoice status. */
export function invoiceStatusVariant(status: string): BadgeVariant {
  switch (status) {
    case "paid":
    case "settled":
      return "success";
    case "refunded":
      return "warning";
    case "not_charged":
      return "neutral";
    default:
      return "default";
  }
}

/** Badge color for a dispute status. */
export function disputeStatusVariant(status: string): BadgeVariant {
  switch (status) {
    case "resolved":
      return "success";
    case "rejected":
      return "danger";
    case "under_review":
      return "warning";
    case "open":
      return "default";
    default:
      return "neutral";
  }
}

/** Badge color for a payout status. */
export function payoutStatusVariant(status: string): BadgeVariant {
  switch (status) {
    case "settled":
    case "paid":
    case "authorized":
      return "success";
    case "rejected":
      return "danger";
    case "requested":
    case "pending":
      return "warning";
    default:
      return "neutral";
  }
}

/** Title-case a snake/space status string for display. */
export function humanizeStatus(status: string | null | undefined): string {
  if (!status) return "—";
  return status.replace(/_/g, " ");
}

/** Format a duration in seconds as a compact human string. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}
