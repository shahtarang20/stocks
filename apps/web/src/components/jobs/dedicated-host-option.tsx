"use client";

import { Label } from "@/components/ui/label";
import { DEDICATED_HELP, DEDICATED_LABEL } from "@/lib/host-kind";

/** The "Only use dedicated computers" checkbox, controlled by its parent. */
export function DedicatedHostOption({
  id,
  checked,
  onChange,
  disabled,
}: {
  id: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-start gap-2">
      <input
        id={id}
        type="checkbox"
        className="mt-1 h-4 w-4"
        checked={checked}
        disabled={disabled}
        aria-describedby={`${id}-help`}
        onChange={(e) => onChange(e.target.checked)}
      />
      <div className="space-y-0.5">
        <Label htmlFor={id}>{DEDICATED_LABEL}</Label>
        <p id={`${id}-help`} className="text-xs text-muted-foreground">
          {DEDICATED_HELP}
        </p>
      </div>
    </div>
  );
}
