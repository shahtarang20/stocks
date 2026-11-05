import { describe, expect, it } from "vitest";
import { safeNextPath } from "./auth-form";

describe("safeNextPath (post-login redirect)", () => {
  it("keeps same-site paths", () => {
    expect(safeNextPath("/dashboard/jobs?x=1")).toBe("/dashboard/jobs?x=1");
  });
  it.each([null, "", "https://evil.example", "//evil.example", "/\\evil.example", "/\t/evil.example", "/\n/evil.example", "evil.example"])(
    "refuses %j",
    (v) => {
      expect(safeNextPath(v)).toBe("/dashboard");
    },
  );
});
