import { describe, expect, it } from "vitest";
import { sandboxHelp, sandboxLabel, sandboxState } from "./host-sandbox";

describe("sandboxState", () => {
  it("is ready when a sandbox tier is reported", () => {
    expect(sandboxState({ supported_isolation_tiers: ["container", "gvisor"] })).toBe("ready");
    expect(sandboxState({ supported_isolation_tiers: ["container", "MicroVM"] })).toBe("ready");
  });
  it("is ordinary when only the plain container is reported", () => {
    expect(sandboxState({ supported_isolation_tiers: ["container"] })).toBe("ordinary");
    expect(sandboxState({ supported_isolation_tiers: ["container", "open"] })).toBe("ordinary");
  });
  it("does not guess when an older agent reports nothing", () => {
    expect(sandboxState({})).toBe("unknown");
    expect(sandboxState({ supported_isolation_tiers: [] })).toBe("unknown");
    expect(sandboxState({ supported_isolation_tiers: null as unknown as string[] })).toBe("unknown");
    expect(sandboxLabel("unknown")).toBe("");
    expect(sandboxHelp("unknown")).toBe("");
  });
  it("uses plain words", () => {
    expect(sandboxLabel("ready")).toBe("Sandbox ready");
    expect(sandboxLabel("ordinary")).toBe("Ordinary jobs only");
    expect(sandboxHelp("ordinary")).toMatch(/gVisor/);
  });
});
