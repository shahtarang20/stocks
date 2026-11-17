"use client";

import { Button } from "@/components/ui/button";
import { PERSONAS, type Persona, type SetupMode } from "@/lib/host-setup";

/** Three big cards for "which one are you?", shown the first time a host opens the setup page. */
export function PersonaCards({ onPick }: { onPick: (persona: Persona) => void }) {
  return (
    <section aria-labelledby="persona-heading" className="space-y-3" data-testid="persona-cards">
      <h2 id="persona-heading" className="text-base font-semibold tracking-tight">Which one are you?</h2>
      <p className="text-sm text-muted-foreground">Pick the closest one. It only changes how much we show you. You can change it at any time.</p>
      <div className="grid gap-3 sm:grid-cols-3">
        {PERSONAS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => onPick(p.id)}
            className="rounded-lg border border-border bg-surface p-4 text-left transition-colors hover:border-primary hover:bg-surface-2/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            <span className="block text-sm font-semibold">{p.title}</span>
            <span className="mt-1 block text-sm text-muted-foreground">{p.text}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

/** A thin bar with the current choice, the switch between Easy and Advanced, and a way back to the three cards. */
export function ModeBar({
  persona,
  mode,
  onMode,
  onChangePersona,
}: {
  persona: Persona;
  mode: SetupMode;
  onMode: (mode: SetupMode) => void;
  onChangePersona: () => void;
}) {
  const title = PERSONAS.find((p) => p.id === persona)?.title ?? "";
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-border bg-surface-2/40 px-4 py-3 text-sm" data-testid="mode-bar">
      <p className="text-muted-foreground">
        You chose: <span className="font-medium text-foreground">{title}</span>.{" "}
        {mode === "easy" ? "You see one step at a time." : "You see every step and command."}
      </p>
      <div className="ml-auto flex flex-wrap gap-2">
        {mode === "easy" ? (
          <Button type="button" variant="outline" size="sm" onClick={() => onMode("advanced")}>
            Show every step and command
          </Button>
        ) : (
          <Button type="button" variant="outline" size="sm" onClick={() => onMode("easy")}>
            Show one step at a time
          </Button>
        )}
        <Button type="button" variant="ghost" size="sm" onClick={onChangePersona}>
          Change my choice
        </Button>
      </div>
    </div>
  );
}
