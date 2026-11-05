/**
 * Display formatting helpers for the console. Pure functions: they never change
 * the underlying value, only how it is shown. Credit amounts are deliberately NOT
 * reformatted here: the API returns exact decimal strings and the console shows them verbatim.
 */

/**
 * Parse an API timestamp. The API stores UTC; some rows serialize without a zone designator
 * ("2026-11-04T18:10:56.66" - e.g. SQLite-backed or naive columns), which `new Date` would read as
 * LOCAL time and show hours off ("6h ago" for a job created a second ago). A date-time string with
 * no "Z"/offset is therefore treated as UTC.
 */
export function parseApiDate(iso: string): Date {
  const naive = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(iso);
  return new Date(naive ? `${iso.replace(" ", "T")}Z` : iso);
}

/** "4 Nov 2026, 10:42" in the viewer's locale and time zone; "—" when missing or invalid. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseApiDate(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "2h 05m", "3m 12s", "45s". Input in seconds; "—" when not a finite number. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** Upper-case the first letter of a status or label. "retry pending" -> "Retry pending". */
export function capitalize(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/** First segment of a long identifier, for compact tables. Full value belongs in a tooltip and copy button. */
export function truncateId(id: string | null | undefined, length = 8): string {
  if (!id) return "—";
  return id.length > length ? id.slice(0, length) : id;
}

/** Plain name for a currency code. Credits are test credits in this preview, never real money. */
export function creditsName(currency?: string | null): string {
  return !currency || currency === "USDC_CREDITS" ? "test credits" : currency;
}
