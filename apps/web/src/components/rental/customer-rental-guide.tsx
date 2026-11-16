import { AlertTriangle, Check, X } from "lucide-react";
import {
  CUSTOMER_RENTAL_BEFORE,
  CUSTOMER_RENTAL_COST,
  CUSTOMER_RENTAL_FIRST,
  CUSTOMER_RENTAL_GOOD_FOR,
  CUSTOMER_RENTAL_IMAGE_HELP,
  CUSTOMER_RENTAL_NOT_FOR,
  CUSTOMER_RENTAL_PROBLEMS,
  CUSTOMER_RENTAL_STEPS,
  CUSTOMER_RENTAL_TEAMS,
  CUSTOMER_RENTAL_WHO,
} from "@/lib/rental";

/**
 * How renting a graphics card works, for every kind of customer, from someone who has never used a terminal to a team that brings its own
 * image. Plain words: which kind of customer you are, the steps, what it costs, what to have ready, the first minutes, how to get an image's
 * address, what it is and is not good for, what to do when something goes wrong, and the extra details for teams. It adds no headings of its
 * own (the page already has one), so the longer parts are folded sections. Shown above the Rent form.
 */
export function CustomerRentalGuide() {
  return (
    <section aria-labelledby="crg-title" className="space-y-4 rounded-lg border border-border bg-surface p-4 sm:p-6" data-testid="customer-rental-guide">
      <div>
        <h2 id="crg-title" className="text-lg font-semibold">How renting a graphics card works</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          You rent a computer with a graphics card (GPU) from someone who shares theirs, and run <em>your own</em> code on it, with JupyterLab or a
          terminal in your browser. You pay per second while it runs. Read this once, then fill in the form below.
        </p>
      </div>

      <div className="space-y-2 text-sm">
        <p className="font-medium">Which one are you?</p>
        <ul className="space-y-2">
          {CUSTOMER_RENTAL_WHO.map((w) => (
            <li key={w.lead} className="text-muted-foreground">
              <span className="font-medium text-foreground">{w.lead}</span> {w.text}
            </li>
          ))}
        </ul>
      </div>

      <ol className="space-y-3 text-sm">
        {CUSTOMER_RENTAL_STEPS.map((s, i) => (
          <li key={s.title} className="flex items-start gap-3">
            <span aria-hidden="true" className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
              {i + 1}
            </span>
            <span>
              <span className="font-medium">{s.title}.</span> <span className="text-muted-foreground">{s.text}</span>
            </span>
          </li>
        ))}
      </ol>

      <details className="rounded-md border border-border px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">What it will cost</summary>
        <p className="mt-2 text-muted-foreground">{CUSTOMER_RENTAL_COST}</p>
      </details>

      <details open className="rounded-md border border-border px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">Before you start</summary>
        <ul className="mt-2 space-y-2">
          {CUSTOMER_RENTAL_BEFORE.map((b) => (
            <li key={b.lead} className="flex items-start gap-2">
              <Check aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-success" />
              <span>
                <span className="font-medium">{b.lead}</span> <span className="text-muted-foreground">{b.text}</span>
              </span>
            </li>
          ))}
        </ul>
      </details>

      <details className="rounded-md border border-border px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">Your first five minutes</summary>
        <ul className="mt-2 space-y-2">
          {CUSTOMER_RENTAL_FIRST.map((f) => (
            <li key={f.lead}>
              <span className="font-medium">{f.lead}</span> <span className="text-muted-foreground">{f.text}</span>
            </li>
          ))}
        </ul>
      </details>

      <details className="rounded-md border border-border px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">How to get the address of your own image</summary>
        <ul className="mt-2 space-y-2">
          {CUSTOMER_RENTAL_IMAGE_HELP.map((f) => (
            <li key={f.lead}>
              <span className="font-medium">{f.lead}</span> <span className="text-muted-foreground">{f.text}</span>
            </li>
          ))}
        </ul>
      </details>

      <details className="rounded-md border border-border px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">What it is good for, and what it is not</summary>
        <div className="mt-2 grid gap-4 sm:grid-cols-2">
          <div>
            <p className="font-medium">Good for</p>
            <ul className="mt-1 space-y-1.5">
              {CUSTOMER_RENTAL_GOOD_FOR.map((t) => (
                <li key={t} className="flex items-start gap-2 text-muted-foreground">
                  <Check aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-success" /> <span>{t}</span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <p className="flex items-center gap-1.5 font-medium">
              <AlertTriangle aria-hidden="true" className="h-4 w-4 text-warning" /> Not for
            </p>
            <ul className="mt-1 space-y-1.5">
              {CUSTOMER_RENTAL_NOT_FOR.map((t) => (
                <li key={t} className="flex items-start gap-2 text-muted-foreground">
                  <X aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-danger" /> <span>{t}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </details>

      <details className="rounded-md border border-border px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">If something goes wrong</summary>
        <ul className="mt-2 space-y-2">
          {CUSTOMER_RENTAL_PROBLEMS.map((p) => (
            <li key={p.lead}>
              <span className="font-medium">{p.lead}</span> <span className="text-muted-foreground">{p.text}</span>
            </li>
          ))}
        </ul>
      </details>

      <details className="rounded-md border border-border px-4 py-3 text-sm">
        <summary className="cursor-pointer font-medium">For teams and heavy use</summary>
        <ul className="mt-2 space-y-2">
          {CUSTOMER_RENTAL_TEAMS.map((f) => (
            <li key={f.lead}>
              <span className="font-medium">{f.lead}</span> <span className="text-muted-foreground">{f.text}</span>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
