"use client";

import { AlertTriangle, Check, Info } from "lucide-react";
import { CopyField } from "@/components/provider/copy-field";
import {
  HOST_RENTAL_CARE,
  HOST_RENTAL_COMMANDS,
  HOST_RENTAL_DATACENTRE,
  HOST_RENTAL_EARN,
  HOST_RENTAL_NEEDS,
  HOST_RENTAL_NO_CARD,
  HOST_RENTAL_RISKS,
  HOST_RENTAL_TROUBLE,
  HOST_RENTAL_WHAT_HAPPENS,
  HOST_RENTAL_WHO,
  HOST_RENTAL_WORKING,
} from "@/lib/rental";

function Code({ children }: { children: React.ReactNode }) {
  return <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-foreground">{children}</code>;
}

/**
 * Everything a host who has never heard of container rental needs, from someone with a gaming laptop to someone running a data centre:
 * what it is and how they earn, which kind of host they are, what the computer needs, the exact commands in order, how to tell it works,
 * what happens while a customer rents it, how to look after the computer, the honest risks, what to do when something fails, the extra
 * details for many computers, and how to turn it off. Shown on the host setup page and on "My computers".
 * `headingLevel` keeps the heading order right wherever it is placed.
 */
export function HostRentalGuide({ headingLevel = 3 }: { headingLevel?: 3 | 4 }) {
  const H = (headingLevel === 3 ? "h3" : "h4") as "h3" | "h4";
  return (
    <div className="space-y-5 text-sm" data-testid="host-rental-guide">
      <div className="space-y-2 text-muted-foreground">
        <p>
          <strong className="font-medium text-foreground">What it is.</strong> Most jobs on ComputeCoin run software we publish. With{" "}
          <strong className="font-medium text-foreground">container rental</strong> on, a customer can instead run <em>their own</em> code on your
          computer, in JupyterLab or a terminal, for work such as training or fine-tuning a model. It is off until you turn it on, and you can
          turn it off at any time.
        </p>
        <p>
          <strong className="font-medium text-foreground">How you earn.</strong> {HOST_RENTAL_EARN}
        </p>
      </div>

      <section aria-labelledby="hrg-who">
        <H id="hrg-who" className="text-sm font-semibold">Which one are you?</H>
        <ul className="mt-2 space-y-2">
          {HOST_RENTAL_WHO.map((w) => (
            <li key={w.lead} className="text-muted-foreground">
              <span className="font-medium text-foreground">{w.lead}</span> {w.text}
            </li>
          ))}
        </ul>
      </section>

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
        <p className="mt-3 flex items-start gap-2 rounded-md bg-surface-2/50 px-3 py-2 text-xs text-muted-foreground">
          <Info aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{HOST_RENTAL_NO_CARD}</span>
        </p>
      </section>

      <section aria-labelledby="hrg-steps">
        <H id="hrg-steps" className="text-sm font-semibold">Turn it on, in this order</H>
        <p className="mt-2 rounded-md border border-border bg-surface-2/40 px-3 py-2 text-muted-foreground" data-testid="hrg-quickest">
          <strong className="font-medium text-foreground">The quickest way.</strong> If your computer is not connected yet, do it all with one line: in
          step 3c of this page, tick <strong className="font-medium text-foreground">&ldquo;Also let customers run their own code on this computer&rdquo;</strong>{" "}
          and paste the line it shows (or press the <strong className="font-medium text-foreground">&ldquo;Create token and copy the install command&rdquo;</strong> button, which also creates your token). It installs the agent, connects the computer, turns container rental on and tests the computer, and tells you
          what to fix if something is missing. Then set your price (step 4 below). The separate commands below are for a computer that is already
          connected.
        </p>
        <p className="mt-2 text-muted-foreground">Run these in a terminal on the computer that will be rented (the one that has the graphics card).</p>
        <ol className="mt-3 space-y-4">
          <li className="space-y-1.5">
            <p>
              <span className="font-medium">1. Test your computer.</span>{" "}
              <span className="text-muted-foreground">
                This checks Docker, your graphics card, free disk space and a locked-down test container. The first run downloads a small image, so
                it can take a few minutes. If a line says <Code>FAIL</Code>, the message under it says what to do. Fix it, then run the test again.
                Warnings are fine.
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
                This shows whether rental is on for this computer.{" "}
                <strong className="font-medium text-foreground">My computers</strong> shows &ldquo;Container rental: on&rdquo; about a minute after the
                restart.
              </span>
            </p>
            <CopyField value={HOST_RENTAL_COMMANDS.status} />
          </li>
          <li className="space-y-1.5">
            <p>
              <span className="font-medium">4. Set your price and switch to Online.</span>{" "}
              <span className="text-muted-foreground">
                In <strong className="font-medium text-foreground">My computers</strong>, open the menu on your computer and choose{" "}
                <strong className="font-medium text-foreground">Edit price and availability</strong>. A computer with no price, or set to Offline, gets
                no rentals.
              </span>
            </p>
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

      <section aria-labelledby="hrg-working">
        <H id="hrg-working" className="text-sm font-semibold">How you know it is working</H>
        <ul className="mt-2 list-disc space-y-1.5 pl-5 text-muted-foreground">
          {HOST_RENTAL_WORKING.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="hrg-happens">
        <H id="hrg-happens" className="text-sm font-semibold">What happens when a customer rents your computer</H>
        <ul className="mt-2 list-disc space-y-1.5 pl-5 text-muted-foreground">
          {HOST_RENTAL_WHAT_HAPPENS.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="hrg-care">
        <H id="hrg-care" className="text-sm font-semibold">Looking after your computer</H>
        <ul className="mt-2 list-disc space-y-1.5 pl-5 text-muted-foreground">
          {HOST_RENTAL_CARE.map((t) => (
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

      <section aria-labelledby="hrg-trouble">
        <H id="hrg-trouble" className="text-sm font-semibold">If something does not work</H>
        <dl className="mt-2 space-y-3">
          {HOST_RENTAL_TROUBLE.map((t) => (
            <div key={t.problem}>
              <dt className="font-medium">{t.problem}</dt>
              <dd className="text-muted-foreground">{t.fix}</dd>
            </div>
          ))}
        </dl>
      </section>

      <details className="rounded-md border border-border px-4 py-3" data-testid="hrg-datacentre">
        <summary className="cursor-pointer font-medium marker:text-muted-foreground">For data centres and many computers</summary>
        <ul className="mt-3 space-y-2.5">
          {HOST_RENTAL_DATACENTRE.map((d) => (
            <li key={d.lead} className="text-muted-foreground">
              <span className="font-medium text-foreground">{d.lead}</span> {d.text}
            </li>
          ))}
        </ul>
      </details>

      <section aria-labelledby="hrg-off">
        <H id="hrg-off" className="text-sm font-semibold">Turn it off</H>
        <p className="mt-1 text-muted-foreground">
          Run the command below, then restart the agent. A rental that is already running is allowed to finish. No new ones start. Your computer
          keeps taking Kits and ordinary jobs. To pause for a while instead, set the computer to Offline in My computers.
        </p>
        <div className="mt-2">
          <CopyField value={HOST_RENTAL_COMMANDS.off} />
        </div>
      </section>
    </div>
  );
}
