import { describe, expect, it } from "vitest";
import type { MachineLive, MachineUsage } from "@/lib/api";
import { formatDuration, liveStatusLine, memoryPercent, outcomeRows, percentOf, tempTone, usageSlices } from "./host-usage";

const usage = (over: Partial<MachineUsage["seconds"]> = {}, telemetry = true): MachineUsage => ({
  machine_id: "m",
  days: 7,
  window_start: "2026-11-07T00:00:00Z",
  window_end: "2026-11-14T00:00:00Z",
  telemetry_available: telemetry,
  seconds: { window: 1000, running: 250, idle: 600, not_reporting: 150, ...over },
  attempts: {},
});

describe("formatDuration", () => {
  it("is plain and rounded", () => {
    expect(formatDuration(0)).toBe("0 min");
    expect(formatDuration(20)).toBe("less than a minute");
    expect(formatDuration(45 * 60)).toBe("45 min");
    expect(formatDuration(3 * 3600 + 20 * 60)).toBe("3 h 20 min");
    expect(formatDuration(2 * 86400 + 3 * 3600)).toBe("2 d 3 h");
  });
  it("never invents a number", () => {
    for (const bad of [null, undefined, -5, NaN, Infinity]) expect(formatDuration(bad as number)).toBe("–");
  });
});

describe("usageSlices", () => {
  it("splits the whole period three ways when the computer reported", () => {
    const s = usageSlices(usage());
    expect(s.map((x) => x.percent)).toEqual([25, 60, 15]);
  });
  it("shows only the busy part when there were no readings, never a made-up split", () => {
    const s = usageSlices(usage({ idle: null, not_reporting: null }, false));
    expect(s.map((x) => x.name)).toEqual(["Running customer jobs"]);
  });
  it("leaves out empty slices and handles nothing at all", () => {
    expect(usageSlices(usage({ running: 0 })).map((x) => x.name)).not.toContain("Running customer jobs");
    expect(usageSlices(undefined)).toEqual([]);
  });
});

describe("outcomeRows", () => {
  it("uses plain words in a fixed order and hides zeros", () => {
    const rows = outcomeRows({ failed: 1, completed: 4, lost: 0 });
    expect(rows.map((r) => [r.label, r.count])).toEqual([["Finished", 4], ["Did not finish", 1]]);
  });
  it("folds unknown outcomes into Other", () => {
    const rows = outcomeRows({ other: 1, brand_new: 2 });
    expect(rows).toEqual([expect.objectContaining({ key: "other", count: 3 })]);
    expect(outcomeRows({ brand_new: 2 })[0].label).toBe("Other");
    expect(outcomeRows(undefined)).toEqual([]);
  });
});

describe("small helpers", () => {
  it("percentages are bounded and safe", () => {
    expect(percentOf(1, 0)).toBe(0);
    expect(percentOf(5, 2)).toBe(100);
    expect(memoryPercent(2048, 8192)).toBe(25);
    expect(memoryPercent(null, 8192)).toBeNull();
    expect(memoryPercent(1, 0)).toBeNull();
  });
  it("temperature tones", () => {
    expect(tempTone(60)).toBe("ok");
    expect(tempTone(78)).toBe("warm");
    expect(tempTone(90)).toBe("hot");
    expect(tempTone(null)).toBe("unknown");
  });
  it("the live line never calls old numbers live", () => {
    const base = { readings_recent: false, last_reading_at: null } as MachineLive;
    expect(liveStatusLine({ ...base, readings_recent: true })).toMatch(/^Live/);
    expect(liveStatusLine({ ...base, last_reading_at: "2026-11-14T00:00:00Z" })).toMatch(/No recent readings/);
    expect(liveStatusLine(base)).toMatch(/not sent any/);
    expect(liveStatusLine(undefined)).toBe("");
  });
});
