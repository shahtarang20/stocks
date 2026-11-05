import { cn } from "@/lib/utils";

export function Prose({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "max-w-3xl space-y-4 text-[0.9375rem] leading-7 text-muted-foreground",
        "[&>:first-child]:mt-0 [&_h2]:mt-10 [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:leading-snug [&_h2]:tracking-tight [&_h2]:text-foreground [&_h3]:mt-6 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-foreground",
        "[&_a]:rounded-sm [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-2 [&_a:hover]:no-underline [&_strong]:font-semibold [&_strong]:text-foreground",
        "[&_ul]:list-disc [&_ul]:space-y-1.5 [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:space-y-1.5 [&_ol]:pl-5 [&_li]:pl-1 [&_li]:marker:text-muted-foreground",
        "[&_code]:rounded [&_code]:bg-surface-2 [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.85em] [&_code]:text-foreground",
        "[&_table]:w-full [&_table]:min-w-[28rem] [&_table]:border-collapse [&_table]:text-sm [&_th]:border-b [&_th]:border-border [&_th]:py-2 [&_th]:pr-4 [&_th]:text-left [&_th]:text-xs [&_th]:font-semibold [&_th]:uppercase [&_th]:tracking-wide [&_th]:text-foreground [&_td]:border-b [&_td]:border-border [&_td]:py-2.5 [&_td]:pr-4 [&_td]:align-top [&_tr:last-child_td]:border-0 [&_td:first-child]:font-medium [&_td:first-child]:text-foreground",
        className,
      )}
    >
      {children}
    </div>
  );
}
