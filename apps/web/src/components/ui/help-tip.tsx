"use client";

import { useEffect, useId, useRef, useState } from "react";
import { HelpCircle } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A small "What is this?" button for jargon that cannot be avoided.
 * Put it right after the hard word. Click, tap or press Enter to read a short plain
 * explanation; press Escape or click away to close it. The explanation opens in line
 * (not floating), so it never runs off a small screen.
 *
 *   Reservation <HelpTip term="reservation">Credits set aside for a job until it ends.</HelpTip>
 */
export function HelpTip({
  term,
  children,
  className,
}: {
  /** The word being explained. Used in the button label: "What is <term>?" */
  term: string;
  /** One or two short, plain sentences. */
  children: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const root = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  return (
    <span ref={root} className={cn("inline", className)}>
      <button
        type="button"
        aria-label={`What is ${term}?`}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
        className="ml-1 inline-flex h-5 w-5 translate-y-[3px] items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary"
      >
        <HelpCircle aria-hidden="true" className="h-4 w-4" />
      </button>
      {open ? (
        <span
          id={id}
          role="note"
          className="mt-1.5 block max-w-prose rounded-md border border-border bg-surface-2 px-3 py-2 text-xs font-normal normal-case leading-5 tracking-normal text-foreground"
        >
          {children}
        </span>
      ) : null}
    </span>
  );
}
