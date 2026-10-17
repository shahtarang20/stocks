"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Monospace value with a copy-to-clipboard button. Used for secrets and
 * agent commands on the provider onboarding flow.
 */
export function CopyField({
  value,
  label,
  className,
  monospace = true,
}: {
  value: string;
  label?: string;
  className?: string;
  monospace?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard may be unavailable (insecure context) — no-op. */
    }
  }

  return (
    <div className={cn("space-y-1.5", className)}>
      {label ? (
        <p className="text-xs font-medium text-muted-foreground">{label}</p>
      ) : null}
      <div className="flex items-stretch gap-2">
        <code
          className={cn(
            "flex min-w-0 flex-1 items-center overflow-x-auto rounded-md border border-border bg-surface-2 px-3 py-2 text-sm",
            monospace && "font-mono",
          )}
        >
          <span className="whitespace-pre">{value}</span>
        </code>
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label={copied ? "Copied" : "Copy to clipboard"}
          onClick={copy}
        >
          {copied ? (
            <Check className="text-success" />
          ) : (
            <Copy />
          )}
        </Button>
      </div>
    </div>
  );
}
