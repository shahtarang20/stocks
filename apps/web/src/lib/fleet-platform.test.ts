// W1 / W10 (cross-platform plan): admin fleet view helpers. WRITTEN, NOT RUN.
// Bug re-insertion: make platformOf() treat a missing platform as "linux" (the "unreported" tests fail); drop the filter
// (the filter test fails); return raw flag names (the label test fails).
import { describe, expect, it } from "vitest";
import {
  CAPABILITY_LABELS,
  capabilityLabels,
  filterByPlatform,
  platformCounts,
  platformLabel,
  platformOf,
} from "./fleet-platform";

const linux = { platform: { os: "linux", os_version: "6.8", runtime_backend: "linux_docker" }, capabilities: ["container_tier", "nvidia_cuda", "gvisor"] };
const windows = { platform: { os: "windows", os_version: "10.0.22631", runtime_backend: "wsl_engine" }, capabilities: ["container_tier", "nvidia_cuda"] };
const legacy = {};

describe("fleet platform helpers", () => {
  it("buckets machines by reported OS; a machine with no platform object is unreported, never assumed Linux", () => {
    expect(platformOf(linux)).toBe("linux");
    expect(platformOf(windows)).toBe("windows");
    expect(platformOf(legacy)).toBe("unreported");
    expect(platformOf({ platform: null })).toBe("unreported");
    expect(platformOf({ platform: { os: "plan9" } })).toBe("unreported");
  });

  it("filters by platform", () => {
    const all = [linux, windows, legacy];
    expect(filterByPlatform(all, "all")).toHaveLength(3);
    expect(filterByPlatform(all, "windows")).toEqual([windows]);
    expect(filterByPlatform(all, "linux")).toEqual([linux]);
    expect(filterByPlatform(all, "unreported")).toEqual([legacy]);
  });

  it("counts every bucket", () => {
    expect(platformCounts([linux, windows, windows, legacy])).toEqual({ all: 4, linux: 1, windows: 2, unreported: 1 });
  });

  it("labels the platform in plain language", () => {
    expect(platformLabel(windows)).toBe("Windows 10.0.22631 · wsl_engine");
    expect(platformLabel(linux)).toBe("Linux 6.8 · linux_docker");
    expect(platformLabel(legacy)).toContain("older agent");
  });

  it("shows capability flags with plain labels and keeps unknown names visible", () => {
    expect(capabilityLabels(linux)).toEqual(["Containers", "NVIDIA CUDA", "gVisor"]);
    expect(capabilityLabels({ capabilities: ["future_flag"] })).toEqual(["future_flag"]);
    expect(capabilityLabels(legacy)).toEqual([]);
  });

  it("has a label for every flag in the plan", () => {
    const plan = [
      "container_tier", "nvidia_cuda", "amd_rocm", "gvisor", "microvm", "sealed", "sealed_pro", "confidential",
      "vfio_gpu", "multi_node", "egress_proxy", "thermal_cap", "power_limit", "multi_gpu", "cpu_capable",
    ];
    expect(Object.keys(CAPABILITY_LABELS).sort()).toEqual([...plan].sort());
  });
});
