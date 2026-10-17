import { FlaskConical } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Shared, deliberately prominent banner for any view that renders synthetic
 * data. Honesty constraint: demo data must never be presented as live
 * infrastructure, real benchmarks, real utilization, or token metrics.
 */
export function DemoBanner({
  children,
  className,
}: {
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      role="note"
      className={cn(
        "flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-foreground",
        className,
      )}
    >
      <FlaskConical className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
      <p>
        <span className="font-semibold text-warning">DEMO MODE — </span>
        {children ??
          "synthetic data, not live infrastructure. These rows are hand-authored placeholders and do not represent real availability, benchmarks, or utilization."}
      </p>
    </div>
  );
}
