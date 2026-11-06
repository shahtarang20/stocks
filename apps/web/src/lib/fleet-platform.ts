// W1 / W10 (cross-platform plan): the admin fleet view's platform column and filter. Pure helpers, no React.
// The API already sanitises `platform` and `capabilities` (known keys / known flags only); this file only presents them.

export type PlatformFilter = "all" | "linux" | "windows" | "unreported";

export interface FleetMachineLike {
  platform?: { os?: string; os_version?: string; runtime_backend?: string; agent_version?: string; supervisor_version?: string } | null;
  capabilities?: string[] | null;
}

/** Flag names as the plan defines them (section 3.4), with a plain-language label. */
export const CAPABILITY_LABELS: Record<string, string> = {
  container_tier: "Containers",
  nvidia_cuda: "NVIDIA CUDA",
  amd_rocm: "AMD ROCm",
  gvisor: "gVisor",
  microvm: "microVM",
  sealed: "Sealed",
  sealed_pro: "Sealed Pro",
  confidential: "Confidential",
  vfio_gpu: "Windows VM (GPU passthrough)",
  multi_node: "Multi-node",
  egress_proxy: "Egress proxy",
  thermal_cap: "Thermal cap",
  power_limit: "Power limit",
  multi_gpu: "Multi-GPU",
  cpu_capable: "CPU-only jobs",
};

/** Which OS bucket a machine falls in. A machine that never sent a platform object is "unreported" (an older agent). */
export function platformOf(machine: FleetMachineLike): Exclude<PlatformFilter, "all"> {
  const os = machine.platform?.os;
  if (os === "windows") return "windows";
  if (os === "linux") return "linux";
  return "unreported";
}

export function platformLabel(machine: FleetMachineLike): string {
  const p = machine.platform;
  if (!p) return "Not reported (older agent)";
  const os = p.os === "windows" ? "Windows" : p.os === "linux" ? "Linux" : "Unknown OS";
  const backend = p.runtime_backend ? ` · ${p.runtime_backend}` : "";
  return `${os}${p.os_version ? ` ${p.os_version}` : ""}${backend}`;
}

export function filterByPlatform<T extends FleetMachineLike>(machines: T[], filter: PlatformFilter): T[] {
  return filter === "all" ? machines : machines.filter((m) => platformOf(m) === filter);
}

export function capabilityLabels(machine: FleetMachineLike): string[] {
  return (machine.capabilities ?? []).map((flag) => CAPABILITY_LABELS[flag] ?? flag);
}

export function platformCounts(machines: FleetMachineLike[]): Record<PlatformFilter, number> {
  const counts: Record<PlatformFilter, number> = { all: machines.length, linux: 0, windows: 0, unreported: 0 };
  for (const m of machines) counts[platformOf(m)] += 1;
  return counts;
}
