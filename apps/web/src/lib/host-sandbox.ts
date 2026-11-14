import type { Machine } from "@/lib/api";

export type SandboxState = "ready" | "ordinary" | "unknown";

/**
 * Whether a computer offers a sandbox, from the isolation tiers its agent reported.
 * An older agent that reports nothing is "unknown": the page says nothing rather than guess.
 */
export function sandboxState(machine: Pick<Machine, "supported_isolation_tiers">): SandboxState {
  const tiers = machine.supported_isolation_tiers;
  if (!Array.isArray(tiers) || tiers.length === 0) return "unknown";
  const lower = tiers.map((t) => String(t).toLowerCase());
  return lower.includes("gvisor") || lower.includes("microvm") ? "ready" : "ordinary";
}

export function sandboxLabel(state: SandboxState): string {
  if (state === "ready") return "Sandbox ready";
  if (state === "ordinary") return "Ordinary jobs only";
  return "";
}

export function sandboxHelp(state: SandboxState): string {
  if (state === "ready") return "This computer can also take sandboxed jobs, which customers can ask for when they want extra protection.";
  if (state === "ordinary")
    return "This computer takes ordinary jobs only. Installing gVisor and registering it with Docker lets it take sandboxed jobs as well. See the setup guide.";
  return "";
}
