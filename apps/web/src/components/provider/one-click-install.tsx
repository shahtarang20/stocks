"use client";

import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CopyField } from "@/components/provider/copy-field";

/**
 * The one-click install block, the same for Linux and Windows: one big button that creates a setup token and copies one finished command that
 * already carries it, then the command itself (so it can be copied by hand if the browser refuses). `disabledReason` says why the button cannot
 * be pressed yet. The command has a token in it, so the label says not to share it.
 */
export function OneClickInstall({
  intro,
  command,
  pending,
  note,
  disabledReason,
  onPress,
  commandLabel = "One-click install command (it has your token in it, so do not share it)",
}: {
  intro: React.ReactNode;
  command: string | null;
  pending: boolean;
  note: "copied" | "manual" | null;
  disabledReason: string | null;
  onPress: () => void;
  commandLabel?: string;
}) {
  return (
    <div className="space-y-2 rounded-md border border-border bg-surface-2/40 p-3" data-testid="one-click">
      <p className="text-sm">
        <span className="font-medium">Easiest.</span> <span className="text-muted-foreground">{intro}</span>
      </p>
      <Button type="button" disabled={Boolean(disabledReason) || pending} onClick={onPress}>
        {pending ? (
          <>
            <Loader2 className="animate-spin" /> Creating…
          </>
        ) : command ? (
          "Copy the install command again"
        ) : (
          "Create token and copy the install command"
        )}
      </Button>
      {disabledReason ? <p className="text-xs text-muted-foreground">{disabledReason}</p> : null}
      {command ? (
        <div className="space-y-1.5">
          {note === "copied" ? (
            <p role="status" className="text-xs text-success">Copied. Paste it in the terminal now. It works once and stops working in about an hour.</p>
          ) : note === "manual" ? (
            <p role="status" className="text-xs text-warning">Your browser did not let us copy it. Press the copy button below.</p>
          ) : null}
          <CopyField label={commandLabel} value={command} />
        </div>
      ) : null}
    </div>
  );
}
