/**
 * Dashboard numbers computed from the customer's own jobs and usage records.
 *
 * The dashboard used to show hard-coded demo figures. Everything here is derived
 * from what the API returned; nothing is estimated or invented. Money totals are
 * taken verbatim from the API as decimal strings; only the chart series are
 * converted to numbers, because a chart needs them.
 */
import type { Job, UsageRecord } from "@/lib/api";

export interface DayPoint {
  day: string;
  spend: number;
  hours: number;
}

export interface StatusSlice {
  name: string;
  value: number;
  color: string;
}

const ACTIVE = new Set(["validating", "queued", "matched", "reserved", "running", "verifying"]);

const GROUPS: { name: string; color: string; statuses: string[] }[] = [
  { name: "Succeeded", color: "hsl(152 60% 42%)", statuses: ["completed", "disputed"] },
  { name: "Running", color: "hsl(221 83% 58%)", statuses: ["running", "verifying"] },
  { name: "Queued", color: "hsl(38 92% 50%)", statuses: ["validating", "queued", "matched", "reserved"] },
  { name: "Failed", color: "hsl(0 72% 55%)", statuses: ["failed", "timed_out"] },
  { name: "Cancelled", color: "hsl(215 14% 55%)", statuses: ["cancelled"] },
];

export function jobStatusBreakdown(jobs: Job[]): StatusSlice[] {
  return GROUPS.map((g) => ({
    name: g.name,
    color: g.color,
    value: jobs.filter((j) => g.statuses.includes(j.status)).length,
  })).filter((s) => s.value > 0);
}

export function activeJobCount(jobs: Job[]): number {
  return jobs.filter((j) => ACTIVE.has(j.status)).length;
}

/** The last `days` calendar days (oldest first), bucketed in the viewer's local time. */
export function lastDaysSeries(records: UsageRecord[], days = 7, now = new Date()): DayPoint[] {
  const key = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  const points: (DayPoint & { k: string })[] = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    points.push({
      k: key(d),
      day: d.toLocaleDateString(undefined, { weekday: "short" }),
      spend: 0,
      hours: 0,
    });
  }
  for (const r of records) {
    if (!r.created_at) continue;
    const p = points.find((x) => x.k === key(new Date(r.created_at as string)));
    if (!p) continue;
    p.spend += Number(r.actual_cost ?? 0) || 0;
    p.hours += (Number(r.actual_seconds ?? r.duration_seconds ?? 0) || 0) / 3600;
  }
  return points.map(({ k: _k, ...p }) => ({
    ...p,
    spend: Math.round(p.spend * 100) / 100,
    hours: Math.round(p.hours * 1000) / 1000,
  }));
}
