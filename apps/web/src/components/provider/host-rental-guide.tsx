"use client";

import { AlertTriangle, Check, Info } from "lucide-react";
import { CopyField } from "@/components/provider/copy-field";
import {
  HOST_RENTAL_COMMANDS,
  HOST_RENTAL_NEEDS,
  HOST_RENTAL_RISKS,
  HOST_RENTAL_WHAT_HAPPENS,
} from "@/lib/rental";

function Code({ children }: { children: React.ReactNode }) {
  return <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-foreground">{children}</code>;
}

/**
 * Everything a host who has never heard of container rental needs: what it is, what the computer needs, the exact commands in order,
 * what happens while a customer rents it, the honest risks, and how to turn it off. Shown on the host setup page and on "My computers".
 * `headingLevel` keeps the heading order right wherever it is placed.
 */
export function HostRentalGuide({ headingLevel = 3 }: { headingLevel?: 3 | 4 }) {
  const H = (headingLevel === 3 ? "h3" : "h4") as "h3" | "h4";
  return (
    <div className="space-y-5 text-sm" data-testid="host-rental-guide">
      <p className="text-muted-foreground">
        <strong className="font-medium text-foreground">What it is.</strong> Most jobs on ComputeCoin run software we publish. With{" "}
        <strong className="font-medium text-foreground">container rental</strong> on, a customer can instead run <em>their own</em> code on your
        graphics card, in JupyterLab or a terminal, for work such as training or fine-tuning a model. They pay per second while it runs, and you
        earn that money. It is off until you turn it on, and you can turn it off at any time.
      </p>

      <section aria-labelledby="hrg-needs">
        <H id="hrg-needs" className="text-sm font-semibold">What your computer needs first</H>
        <ul className="mt-2 space-y-2">
          {HOST_RENTAL_NEEDS.map((n) => (
            <li key={n.lead} className="flex items-start gap-2">
              <Check aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-success" />
              <span>
                <span className="font-medium">{n.lead}</span> <span className="text-muted-foreground">{n.text}</span>
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="hrg-steps">
        <H id="hrg-steps" className="text-sm font-semibold">Turn it on, in this order</H>
        <p className="mt-1 text-muted-foreground">Run these in a terminal on the computer that has the graphics card.</p>
        <ol className="mt-3 space-y-4">
          <li className="space-y-1.5">
            <p>
              <span className="font-medium">1. Test your computer.</span>{" "}
              <span className="text-muted-foreground">
                This checks Docker, your graphics card, free disk space and a locked-down test container. The first run downloads a small image, so
                it can take a few minutes. Fix anything marked <Code>FAIL</Code> (the message says what to do), then run it again. Warnings are fine.
              </span>
            </p>
            <CopyField value={HOST_RENTAL_COMMANDS.check} />
          </li>
          <li className="space-y-1.5">
            <p>
              <span className="font-medium">2. Turn it on.</span>{" "}
              <span className="text-muted-foreground">
                It prints what you are agreeing to. Then restart the agent so it takes effect. (If you install with the one-line command, adding{" "}
                <Code>--allow-rental</Code> does this step for you.)
              </span>
            </p>
            <CopyField value={HOST_RENTAL_COMMANDS.on} />
            <CopyField value={HOST_RENTAL_COMMANDS.restart} label="Then restart the agent (if you used the one-line install):" />
          </li>
          <li className="space-y-1.5">
            <p>
              <span className="font-medium">3. Check that it is on.</span>{" "}
              <span className="text-muted-foreground">
                <strong className="font-medium text-foreground">My computers</strong> shows &ldquo;Container rental: on&rdquo; about a minute after the
                restart. Customers can then find your computer by filtering for container rental.
              </span>
            </p>
            <CopyField value={HOST_RENTAL_COMMANDS.status} />
          </li>
        </ol>
        <p className="mt-3 flex items-start gap-2 rounded-md bg-surface-2/50 px-3 py-2 text-xs text-muted-foreground">
          <Info aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            The first rental on a computer downloads a large image, and the customer waits while it does. That is normal. Your own price and
            availability (step 5) decide whether you get rentals.
          </span>
        </p>
      </section>

      <section aria-labelledby="hrg-happens">
        <H id="hrg-happens" className="text-sm font-semibold">What happens when a customer rents your computer</H>
        <ul className="mt-2 list-disc space-y-1.5 pl-5 text-muted-foreground">
          {HOST_RENTAL_WHAT_HAPPENS.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="hrg-risks">
        <H id="hrg-risks" className="flex items-center gap-2 text-sm font-semibold">
          <AlertTriangle aria-hidden="true" className="h-4 w-4 text-warning" /> Know the risks before you turn it on
        </H>
        <ul className="mt-2 list-disc space-y-1.5 pl-5 text-muted-foreground">
          {HOST_RENTAL_RISKS.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="hrg-off">
        <H id="hrg-off" className="text-sm font-semibold">Turn it off</H>
        <p className="mt-1 text-muted-foreground">
          Run the command below, then restart the agent. A rental that is already running is allowed to finish. No new ones start. Your computer
          keeps taking Kits and ordinary jobs.
        </p>
        <div className="mt-2">
          <CopyField value={HOST_RENTAL_COMMANDS.off} />
        </div>
      </section>
    </div>
  );
}
