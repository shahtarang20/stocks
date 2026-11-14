import type { MachineLive, MachineUsage } from "@/lib/api";
import type { StatusSlice } from "@/lib/dashboard-stats";

/** "3 h 20 min", "45 min", "less than a minute". Whole numbers only: these are estimates, not a bill. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "–";
  if (seconds < 60) return seconds === 0 ? "0 min" : "less than a minute";
  const totalMinutes = Math.round(seconds / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  const parts: string[] = [];
  if (days) parts.push(`${days} d`);
  if (hours) parts.push(`${hours} h`);
  if (minutes && !days) parts.push(`${minutes} min`);
  return parts.length ? parts.join(" ") : "0 min";
}

export function percentOf(part: number, whole: number): number {
  if (!whole || whole <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((part / whole) * 100)));
}

const COLORS = {
  running: "hsl(var(--primary))",
  idle: "hsl(var(--muted-foreground))",
  notReporting: "hsl(var(--border))",
} as const;

export interface UsageSlice extends StatusSlice {
  seconds: number;
  percent: number;
}

/**
 * The slices of the "how your computer's time was used" donut.
 * With no readings from the computer there is no honest split of the rest of the time, so only the busy part is returned
 * and the page says why the rest is not broken down.
 */
export function usageSlices(usage: MachineUsage | undefined): UsageSlice[] {
  if (!usage) return [];
  const { window, running, idle, not_reporting } = usage.seconds;
  const rows: Array<{ name: string; seconds: number; color: string }> = [
    { name: "Running customer jobs", seconds: running, color: COLORS.running },
  ];
  if (usage.telemetry_available && idle != null && not_reporting != null) {
    rows.push({ name: "Ready, waiting for jobs", seconds: idle, color: COLORS.idle });
    rows.push({ name: "Not reporting", seconds: not_reporting, color: COLORS.notReporting });
  }
  return rows
    .filter((r) => r.seconds > 0)
    .map((r) => ({ name: r.name, value: r.seconds, color: r.color, seconds: r.seconds, percent: percentOf(r.seconds, window) }));
}

/** Job outcomes in plain words, in the order a host cares about. Unknown keys are shown as "Other". */
const OUTCOME_ORDER: Array<[string, string, string]> = [
  ["completed", "Finished", "These earned you credits."],
  ["running", "Running now", "Still in progress."],
  ["failed", "Did not finish", "The job itself failed; the customer was not charged."],
  ["stopped_here", "Stopped on your computer", "You paused it, or the computer got too hot."],
  ["lost", "Lost contact", "Your computer went quiet while the job was running."],
  ["too_long", "Ran out of time", "The job went past its time limit."],
  ["cancelled", "Cancelled by the customer", "No action needed."],
  ["other", "Other", ""],
];

export interface OutcomeRow {
  key: string;
  label: string;
  hint: string;
  count: number;
}

export function outcomeRows(attempts: Record<string, number> | undefined): OutcomeRow[] {
  const left = { ...(attempts ?? {}) };
  const rows: OutcomeRow[] = [];
  for (const [key, label, hint] of OUTCOME_ORDER) {
    const count = Number(left[key] ?? 0);
    delete left[key];
    if (count > 0) rows.push({ key, label, hint, count });
  }
  const leftover = Object.values(left).reduce((n, v) => n + (Number(v) > 0 ? Number(v) : 0), 0);
  if (leftover > 0) {
    const other = rows.find((r) => r.key === "other");
    if (other) other.count += leftover;
    else rows.push({ key: "other", label: "Other", hint: "", count: leftover });
  }
  return rows;
}

export type TempTone = "ok" | "warm" | "hot" | "unknown";

export function tempTone(temp: number | null | undefined): TempTone {
  if (temp == null || !Number.isFinite(temp)) return "unknown";
  if (temp >= 85) return "hot";
  if (temp >= 75) return "warm";
  return "ok";
}

export function tempWords(tone: TempTone): string {
  return { ok: "Normal", warm: "Warm", hot: "Very hot", unknown: "No reading" }[tone];
}

export function memoryPercent(used: number | null, total: number | null): number | null {
  if (used == null || total == null || total <= 0) return null;
  return percentOf(used, total);
}

/** The line shown above the live numbers. Old numbers are never presented as live. */
export function liveStatusLine(live: MachineLive | undefined): string {
  if (!live) return "";
  if (live.readings_recent) return "Live: updated every few seconds while this page is open.";
  if (live.last_reading_at) return "No recent readings. Your computer has not sent GPU numbers in the last 10 minutes.";
  return "Your computer has not sent any GPU readings yet.";
}
