"use client";

/**
 * Small presentational building blocks shared by the console pages:
 * relative-with-absolute times, truncated ids with copy, empty/error states and
 * table skeletons. Presentation only; they never change data or API calls.
 */

import { useState, type ComponentType, type ReactNode } from "react";
import { AlertCircle, Check, Copy, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/console/toast";
import { formatDateTime } from "@/lib/format";
import { formatRelativeTime, cn } from "@/lib/utils";

/** Relative time ("3h ago") with the exact local date and time on hover and in the DOM. */
export function RelativeTime({
  iso,
  className,
  empty = "—",
}: {
  iso: string | null | undefined;
  className?: string;
  /** Text for a missing value. */
  empty?: string;
}) {
  if (!iso) return <span className={className}>{empty}</span>;
  return (
    <time dateTime={iso} title={formatDateTime(iso)} className={cn("whitespace-nowrap", className)}>
      {formatRelativeTime(iso)}
    </time>
  );
}

/** Icon button that copies `value` to the clipboard and confirms with a toast. */
export function CopyButton({
  value,
  label = "Copy",
  className,
}: {
  value: string;
  /** Accessible name, e.g. "Copy job id". */
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const toast = useToast();

  async function copy(e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      toast.success("Copied to clipboard.");
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Could not copy. Select the text and copy it manually.");
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      className={cn(
        "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    >
      {copied ? <Check aria-hidden="true" className="h-3.5 w-3.5 text-success" /> : <Copy aria-hidden="true" className="h-3.5 w-3.5" />}
    </button>
  );
}

/** Truncated monospace id; the full value is in the tooltip and behind the copy button. */
export function CopyId({
  id,
  length = 8,
  label = "Copy id",
  className,
}: {
  id: string | null | undefined;
  length?: number;
  label?: string;
  className?: string;
}) {
  if (!id) return <span className="text-muted-foreground">—</span>;
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <span title={id} className="font-mono text-xs">
        {id.length > length ? id.slice(0, length) : id}
      </span>
      <CopyButton value={id} label={label} />
    </span>
  );
}

/** Dashed empty state with a clear next action. */
export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
}: {
  icon?: ComponentType<{ className?: string }>;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Card className="border-dashed">
      <CardContent className="flex flex-col items-center gap-3 p-10 text-center sm:p-12">
        {Icon ? (
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
            <Icon className="h-6 w-6" />
          </span>
        ) : null}
        <h3 className="font-semibold">{title}</h3>
        {children ? <p className="max-w-md text-sm text-muted-foreground">{children}</p> : null}
        {action ? <div className="mt-1 flex flex-wrap justify-center gap-2">{action}</div> : null}
      </CardContent>
    </Card>
  );
}

/** Error state with a retry button. Pass the raw error message through `message`. */
export function ErrorState({
  title,
  message,
  onRetry,
  retrying,
}: {
  title: string;
  message?: string;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  return (
    <Card className="border-danger/40">
      <CardContent role="alert" className="flex flex-col items-center gap-3 p-10 text-center">
        <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
          <AlertCircle aria-hidden="true" className="h-5 w-5" />
        </span>
        <h3 className="font-semibold">{title}</h3>
        <p className="max-w-md text-sm text-muted-foreground">{message || "Something went wrong on our side. Try again in a moment. If it keeps happening, come back a bit later."}</p>
        {onRetry ? (
          <Button variant="outline" onClick={onRetry} disabled={retrying}>
            <RefreshCw className={retrying ? "animate-spin" : undefined} /> Try again
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** Skeleton rows sized like a table so loading does not shift the layout. */
export function TableSkeleton({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <Card>
      <CardContent role="status" aria-label={label} className="space-y-3 p-6">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex items-center gap-4">
            <Skeleton className="h-8 w-24" />
            <Skeleton className="h-4 flex-1" />
            <Skeleton className="hidden h-4 w-20 sm:block" />
            <Skeleton className="h-6 w-16 rounded-full" />
          </div>
        ))}
        <span className="sr-only">{label}…</span>
      </CardContent>
    </Card>
  );
}
