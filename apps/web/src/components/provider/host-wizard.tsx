"use client";

import Link from "next/link";
import { AlertTriangle, Check, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ComputerKind, StepId, WizardStep } from "@/lib/host-setup";

const KIND_NOTE: Record<ComputerKind, string> = {
  linux: "This browser looks like it is on a Linux computer. If you want to share a different computer, pick it here.",
  windows: "This browser looks like it is on a Windows computer. If you want to share a different computer, pick it here.",
  mac: "This browser looks like it is on a Mac. A Mac cannot share a graphics card on ComputeCoin. If you have a Linux or Windows 11 computer to share, pick it here.",
  other: "This browser looks like it is on a phone, a tablet or another device that cannot share a graphics card. If you have a Linux or Windows 11 computer to share, pick it here.",
};

function Marker({ n, status }: { n: number; status: WizardStep["status"] }) {
  const base = "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold";
  if (status === "done") {
    return (
      <span className={`${base} bg-success/15 text-success`}>
        <Check aria-hidden="true" className="h-4 w-4" />
        <span className="sr-only">Done</span>
      </span>
    );
  }
  if (status === "blocked") {
    return (
      <span className={`${base} bg-warning/15 text-warning`}>
        <AlertTriangle aria-hidden="true" className="h-4 w-4" />
        <span className="sr-only">Needs attention</span>
      </span>
    );
  }
  return <span className={`${base} ${status === "current" ? "bg-primary text-primary-foreground" : "bg-surface-2 text-muted-foreground"}`}>{n}</span>;
}

/**
 * Easy view of the host setup: five steps, one big button each. The status of every step comes from the server (a computer appeared, it has a price
 * and is Online), so a step ticks itself. Nothing here talks to the server: the page hands in the steps and the pieces that do the work.
 */
export function HostWizard({
  steps,
  detected,
  kind,
  onPickKind,
  checking,
  onBecomeHost,
  becomingHost,
  becomeHostError,
  installSlot,
  rentalBox,
  rentalLine,
}: {
  steps: WizardStep[];
  detected: ComputerKind;
  kind: ComputerKind;
  onPickKind: (kind: "linux" | "windows") => void;
  checking: boolean;
  onBecomeHost: () => void;
  becomingHost: boolean;
  becomeHostError: React.ReactNode;
  installSlot: React.ReactNode;
  /** The container-rental tick box (Linux only: the Windows panel has its own). Null when not shown. */
  rentalBox: React.ReactNode;
  rentalLine: string | null;
}) {
  const body = (id: StepId, step: WizardStep): React.ReactNode => {
    switch (id) {
      case "computer":
        return (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2" role="group" aria-label="Kind of computer you want to share">
              <Button type="button" variant={kind === "linux" ? "default" : "outline"} aria-pressed={kind === "linux"} onClick={() => onPickKind("linux")}>
                Linux
              </Button>
              <Button type="button" variant={kind === "windows" ? "default" : "outline"} aria-pressed={kind === "windows"} onClick={() => onPickKind("windows")}>
                Windows 11 (experimental)
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{KIND_NOTE[detected]}</p>
          </div>
        );
      case "hosting":
        return step.status === "done" ? null : (
          <div className="space-y-2">
            {becomeHostError}
            <Button type="button" onClick={onBecomeHost} disabled={becomingHost}>
              {becomingHost ? (
                <>
                  <Loader2 className="animate-spin" /> Turning on…
                </>
              ) : (
                "Become a host"
              )}
            </Button>
            <p className="text-xs text-muted-foreground">Your account keeps working as a customer account too. It is safe to press this more than once.</p>
          </div>
        );
      case "install":
        // Only the current step shows the buttons. Blocked (Windows installer not published) shows just the reason in the sentence above.
        return step.status === "current" ? (
          <div className="space-y-3">
            {rentalBox}
            {installSlot}
          </div>
        ) : null;
      case "price":
        return step.status === "current" ? (
          <Button asChild>
            <Link href="/provider/hardware">Open my computers</Link>
          </Button>
        ) : null;
      case "live":
        return step.status === "done" ? (
          <div className="space-y-2">
            {rentalLine ? <p className="text-xs text-muted-foreground">{rentalLine}</p> : null}
            <Button asChild variant="outline">
              <Link href="/provider/earnings">See my earnings</Link>
            </Button>
          </div>
        ) : null;
    }
  };

  return (
    <section aria-labelledby="wizard-heading" className="space-y-4" data-testid="host-wizard">
      <h2 id="wizard-heading" className="text-base font-semibold tracking-tight">Share your computer in five steps</h2>
      {checking ? (
        <p role="status" className="text-xs text-muted-foreground">Checking which of your computers are connected…</p>
      ) : null}
      <ol className="space-y-3">
        {steps.map((s, i) => (
          <li
            key={s.id}
            data-step={s.id}
            data-status={s.status}
            aria-current={s.status === "current" || s.status === "blocked" ? "step" : undefined}
            className={`rounded-lg border p-4 ${s.status === "current" ? "border-primary" : s.status === "blocked" ? "border-warning/50" : "border-border"} ${s.status === "upcoming" ? "opacity-70" : ""}`}
          >
            <div className="flex items-start gap-3">
              <Marker n={i + 1} status={s.status} />
              <div className="min-w-0 flex-1 space-y-2">
                <h3 className="text-sm font-semibold leading-tight">{s.title}</h3>
                <p className={`text-sm ${s.status === "blocked" ? "text-foreground" : "text-muted-foreground"}`}>{s.detail}</p>
                {body(s.id, s)}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
