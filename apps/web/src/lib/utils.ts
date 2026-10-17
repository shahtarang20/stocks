import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind class names, de-duplicating conflicting utilities. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Format a USD value with no currency promise beyond display. */
export function formatUsd(value: number, fractionDigits = 2): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

/** Format an ISO timestamp as a compact relative time (e.g. "3m ago"). */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diffMs = Date.now() - then;
  const future = diffMs < 0;
  const abs = Math.abs(diffMs);
  const sec = Math.round(abs / 1000);
  const suffix = future ? "from now" : "ago";
  if (sec < 45) return future ? "in a few seconds" : "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ${suffix}`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ${suffix}`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day}d ${suffix}`;
  const mon = Math.round(day / 30);
  if (mon < 12) return `${mon}mo ${suffix}`;
  const yr = Math.round(mon / 12);
  return `${yr}y ${suffix}`;
}

/** Render a megabyte count as GB (1 decimal), or "—" when unknown/zero. */
export function formatMbAsGb(mb: number | null | undefined): string {
  if (!mb || mb <= 0) return "—";
  return `${(mb / 1024).toFixed(1)} GB`;
}
