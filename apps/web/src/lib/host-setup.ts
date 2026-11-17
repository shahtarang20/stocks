/**
 * Pure logic behind the host setup page's guided (Easy) view: what kind of computer this browser is on, which view to start in, and what state each
 * step is in, worked out from what the server really says (a computer appeared, is priced, is online). Nothing here touches the network or the page.
 */
import type { Machine } from "@/lib/api";
import { machineRentalState } from "@/lib/rental";

export type ComputerKind = "linux" | "windows" | "mac" | "other";

/** A guess from the browser, only used to preselect a choice the host can always change. Phones and Chromebooks are "other" (an Android phone says Linux). */
export function detectComputerKind(userAgent?: string | null): ComputerKind {
  const ua = typeof userAgent === "string" ? userAgent : "";
  if (/Android|iPhone|iPad|iPod|CrOS|Mobile/i.test(ua)) return "other";
  if (/Windows/i.test(ua)) return "windows";
  if (/Macintosh|Mac OS X/i.test(ua)) return "mac";
  if (/Linux|X11|Ubuntu|Debian|Fedora/i.test(ua)) return "linux";
  return "other";
}

export type Persona = "one_pc" | "server" | "many";
export type SetupMode = "easy" | "advanced";

export const PERSONAS: { id: Persona; title: string; text: string }[] = [
  { id: "one_pc", title: "I have one computer", text: "A gaming PC, a laptop or a desktop. We guide you one step at a time." },
  { id: "server", title: "I have a server", text: "A computer that stays on. You see every command and setting." },
  { id: "many", title: "I run many machines", text: "A data centre or a fleet. You see every command, and the tools for setting up many at once." },
];

export function isPersona(v: unknown): v is Persona {
  return v === "one_pc" || v === "server" || v === "many";
}

export function isSetupMode(v: unknown): v is SetupMode {
  return v === "easy" || v === "advanced";
}

/** One computer starts in the guided view; a server or a fleet starts where every command and setting is. */
export function defaultModeFor(persona: Persona): SetupMode {
  return persona === "one_pc" ? "easy" : "advanced";
}

export type StepId = "computer" | "hosting" | "install" | "price" | "live";
export type StepStatus = "done" | "current" | "upcoming" | "blocked";

export interface WizardStep {
  id: StepId;
  title: string;
  status: StepStatus;
  /** One plain sentence for this step in its current state. */
  detail: string;
}

type MachineFacts = Pick<Machine, "status" | "availability" | "asking_price_per_hour" | "capabilities" | "agent_version">;

export interface WizardInput {
  kind: ComputerKind;
  isProvider: boolean;
  /** For Windows: whether the installer has a published address and checksum. Linux does not need one. */
  windowsInstallerReady: boolean;
  machines: MachineFacts[];
}

const isPriced = (m: MachineFacts) => m.asking_price_per_hour !== null && m.asking_price_per_hour !== undefined && m.asking_price_per_hour !== "";
const takesWork = (m: MachineFacts) => isPriced(m) && m.availability === "online";

/** The five steps, each with a status. The first step that is not done is "current" (or "blocked" with the reason), the rest are "upcoming". */
export function wizardSteps(input: WizardInput): WizardStep[] {
  const supported = input.kind === "linux" || input.kind === "windows";
  const hasMachine = input.machines.length > 0;
  const priced = input.machines.some(takesWork);
  const live = input.machines.some((m) => takesWork(m) && m.status === "online");
  const windowsBlocked = input.kind === "windows" && !input.windowsInstallerReady;

  const defs: { id: StepId; title: string; done: boolean; blocked?: string; doing: string; finished: string }[] = [
    {
      id: "computer",
      title: "Pick your computer",
      done: supported,
      blocked: supported ? undefined : "This kind of computer cannot share a graphics card yet. Use a Linux computer, or a Windows 11 PC (experimental).",
      doing: "Choose Linux or Windows 11 for the computer you want to share.",
      finished: input.kind === "windows" ? "Your computer runs Windows 11 (experimental)." : "Your computer runs Linux.",
    },
    { id: "hosting", title: "Turn on hosting", done: input.isProvider, doing: "Press the button to make your account a host account.", finished: "Hosting is on for your account." },
    {
      id: "install",
      title: "Install and connect your computer",
      done: hasMachine,
      blocked: windowsBlocked ? "The Windows installer is not published yet, so there is nothing to install. Use a Linux computer for now." : undefined,
      doing: "Press the button, paste what it copied into a terminal on that computer, and wait about a minute.",
      finished: input.machines.length === 1 ? "Your computer is connected." : `${input.machines.length} computers are connected.`,
    },
    {
      id: "price",
      title: "Set your price and switch to Online",
      done: priced,
      doing: "Open My computers, choose Edit price and availability, set a price per hour and choose Online.",
      finished: "Your computer has a price and is set to Online.",
    },
    {
      id: "live",
      title: "You are live",
      done: live,
      doing: "Waiting for your computer to report in. This can take a minute after you save the price.",
      finished: "Your computer is online and can take work.",
    },
  ];

  let firstOpen = true;
  return defs.map((d) => {
    if (d.done) return { id: d.id, title: d.title, status: "done" as const, detail: d.finished };
    if (!firstOpen) return { id: d.id, title: d.title, status: "upcoming" as const, detail: d.doing };
    firstOpen = false;
    return d.blocked ? { id: d.id, title: d.title, status: "blocked" as const, detail: d.blocked } : { id: d.id, title: d.title, status: "current" as const, detail: d.doing };
  });
}

/** For the "Allow container rental" line: whether the first computer reports the rental flag yet. */
export function rentalLine(machines: MachineFacts[], wantsRental: boolean): string | null {
  if (!wantsRental || machines.length === 0) return null;
  const state = machineRentalState(machines[0]);
  if (state === "on") return "Container rental is on for this computer.";
  if (state === "update") return "This computer's agent is too old for container rental. Update it, then turn rental on.";
  return "Container rental is not on yet. It shows here about a minute after the agent restarts.";
}

// ---- what the host chose last time (kept only in this browser: a convenience, never needed) ----

export const PREFS_KEY = "computecoin.host-setup";

export interface SetupPrefs {
  persona: Persona;
  mode: SetupMode;
}

type Store = Pick<Storage, "getItem" | "setItem">;

/** The browser's storage, or null where it is missing or refuses access (private window, blocked site data). */
function browserStore(): Store | null {
  try {
    return typeof window !== "undefined" && window.localStorage ? window.localStorage : null;
  } catch {
    return null;
  }
}

/** Reads the saved choice. Anything missing, broken or not a known value gives null, so the page just asks again. Never throws. */
export function readPrefs(store: Store | null = browserStore()): SetupPrefs | null {
  try {
    const raw = store?.getItem(PREFS_KEY);
    if (!raw) return null;
    const v: unknown = JSON.parse(raw);
    if (v && typeof v === "object") {
      const { persona, mode } = v as Record<string, unknown>;
      if (isPersona(persona) && isSetupMode(mode)) return { persona, mode };
    }
  } catch {
    /* ignored: a saved choice is only a convenience */
  }
  return null;
}

/** Saves the choice. A failure is ignored: the page works the same without it. */
export function writePrefs(prefs: SetupPrefs, store: Store | null = browserStore()): void {
  try {
    store?.setItem(PREFS_KEY, JSON.stringify({ persona: prefs.persona, mode: prefs.mode }));
  } catch {
    /* ignored */
  }
}
