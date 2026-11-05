import { describe, expect, it } from "vitest";
import { formatDateTime, parseApiDate } from "./format";
import { formatRelativeTime } from "./utils";

describe("parseApiDate", () => {
  it("treats a zone-less API timestamp as UTC", () => {
    expect(parseApiDate("2026-11-04T18:10:56.666825").toISOString()).toBe("2026-11-04T18:10:56.666Z");
    expect(parseApiDate("2026-11-04 18:10:56").toISOString()).toBe("2026-11-04T18:10:56.000Z");
  });
  it("leaves explicit zones alone", () => {
    expect(parseApiDate("2026-11-04T18:10:56Z").toISOString()).toBe("2026-11-04T18:10:56.000Z");
    expect(parseApiDate("2026-11-04T23:40:56+05:30").toISOString()).toBe("2026-11-04T18:10:56.000Z");
  });
  it("keeps date-only strings valid", () => {
    expect(Number.isNaN(parseApiDate("2026-11-04").getTime())).toBe(false);
  });
});

describe("date display of zone-less timestamps", () => {
  it("shows a just-created zone-less timestamp as just now, not hours ago", () => {
    const now = new Date().toISOString().replace("Z", "");
    expect(formatRelativeTime(now)).toBe("just now");
  });
  it("formatDateTime returns a dash for garbage", () => {
    expect(formatDateTime("nope")).toBe("—");
  });
});
