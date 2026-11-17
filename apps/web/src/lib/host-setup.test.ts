import { describe, expect, it } from "vitest";
import { PERSONAS, defaultModeFor, detectComputerKind, isPersona, isSetupMode, rentalLine, wizardSteps, type WizardInput } from "./host-setup";

const M = (over: Record<string, unknown> = {}) =>
  ({ status: "online", availability: "online", asking_price_per_hour: "1.00", capabilities: ["container_tier"], agent_version: "0.2.0", ...over }) as WizardInput["machines"][number];
const input = (over: Partial<WizardInput> = {}): WizardInput => ({ kind: "linux", isProvider: false, windowsInstallerReady: false, machines: [], ...over });
const ids = (steps: ReturnType<typeof wizardSteps>) => steps.map((s) => `${s.id}:${s.status}`).join(" ");

describe("detectComputerKind", () => {
  it.each([
    ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", "windows"],
    ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", "linux"],
    ["Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0", "linux"],
    ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15", "mac"],
  ])("%s", (ua, kind) => expect(detectComputerKind(ua)).toBe(kind));

  it("treats phones, tablets and Chromebooks as 'other' (an Android phone says Linux)", () => {
    for (const ua of [
      "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36",
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
      "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15",
      "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    ]) {
      expect(detectComputerKind(ua), ua).toBe("other");
    }
  });

  it("never throws and says 'other' for anything it cannot read", () => {
    for (const bad of [undefined, null, "", "curl/8.5.0", 42 as unknown as string]) expect(detectComputerKind(bad)).toBe("other");
  });
});

describe("the situation cards and the starting view", () => {
  it("has the three situations, in the order a beginner reads them", () => {
    expect(PERSONAS.map((p) => p.id)).toEqual(["one_pc", "server", "many"]);
    for (const p of PERSONAS) {
      expect(p.title.length).toBeGreaterThan(8);
      expect(p.text.length).toBeGreaterThan(20);
    }
  });
  it("starts one computer in the guided view and a server or fleet where every command is", () => {
    expect(defaultModeFor("one_pc")).toBe("easy");
    expect(defaultModeFor("server")).toBe("advanced");
    expect(defaultModeFor("many")).toBe("advanced");
  });
  it("accepts only real values when reading what was remembered", () => {
    expect(isPersona("one_pc") && isPersona("server") && isPersona("many")).toBe(true);
    for (const bad of ["", "Server", null, undefined, 1, {}]) expect(isPersona(bad)).toBe(false);
    expect(isSetupMode("easy") && isSetupMode("advanced")).toBe(true);
    for (const bad of ["", "EASY", null, undefined, 0]) expect(isSetupMode(bad)).toBe(false);
  });
});

describe("wizardSteps", () => {
  it("begins with hosting, once the computer type is known", () => {
    expect(ids(wizardSteps(input()))).toBe("computer:done hosting:current install:upcoming price:upcoming live:upcoming");
  });

  it("moves down as the server reports progress: host, connected, priced, live", () => {
    expect(ids(wizardSteps(input({ isProvider: true })))).toBe("computer:done hosting:done install:current price:upcoming live:upcoming");
    expect(ids(wizardSteps(input({ isProvider: true, machines: [M({ asking_price_per_hour: null, status: "offline", availability: "offline" })] })))).toBe(
      "computer:done hosting:done install:done price:current live:upcoming",
    );
    expect(ids(wizardSteps(input({ isProvider: true, machines: [M({ status: "offline" })] })))).toBe("computer:done hosting:done install:done price:done live:current");
    expect(ids(wizardSteps(input({ isProvider: true, machines: [M()] })))).toBe("computer:done hosting:done install:done price:done live:done");
  });

  it("does not call a computer priced unless it has a price AND is switched to Online", () => {
    const steps = (m: Record<string, unknown>) => wizardSteps(input({ isProvider: true, machines: [M(m)] })).find((s) => s.id === "price")!.status;
    expect(steps({ asking_price_per_hour: null })).toBe("current");
    expect(steps({ asking_price_per_hour: "" })).toBe("current");
    expect(steps({ availability: "offline" })).toBe("current");
    expect(steps({ availability: "maintenance" })).toBe("current");
    expect(steps({})).toBe("done");
  });

  it("is done as soon as ONE of several computers is live", () => {
    const steps = wizardSteps(input({ isProvider: true, machines: [M({ asking_price_per_hour: null }), M()] }));
    expect(steps.every((s) => s.status === "done")).toBe(true);
    expect(steps[2].detail).toBe("2 computers are connected.");
  });

  it("blocks on a computer that cannot host, with the reason and no later step called current", () => {
    for (const kind of ["mac", "other"] as const) {
      const steps = wizardSteps(input({ kind, isProvider: true }));
      expect(ids(steps)).toBe("computer:blocked hosting:done install:upcoming price:upcoming live:upcoming");
      expect(steps[0].detail).toMatch(/cannot share a graphics card yet/);
    }
  });

  it("blocks Windows at the install step until the installer is published, and says so", () => {
    const blocked = wizardSteps(input({ kind: "windows", isProvider: true, windowsInstallerReady: false }));
    expect(blocked[2].status).toBe("blocked");
    expect(blocked[2].detail).toMatch(/installer is not published yet/);
    expect(blocked[3].status).toBe("upcoming");
    expect(wizardSteps(input({ kind: "windows", isProvider: true, windowsInstallerReady: true }))[2].status).toBe("current");
    expect(wizardSteps(input({ kind: "linux", isProvider: true }))[2].status).toBe("current"); // Linux never needs a published installer
  });

  it("never marks two steps current, and every step has a plain sentence", () => {
    for (const isProvider of [false, true]) {
      for (const machines of [[], [M({ asking_price_per_hour: null })], [M({ status: "offline" })], [M()]]) {
        const steps = wizardSteps(input({ isProvider, machines }));
        expect(steps.filter((s) => s.status === "current" || s.status === "blocked").length).toBeLessThanOrEqual(1);
        for (const s of steps) expect(s.detail.trim().length).toBeGreaterThan(15);
      }
    }
  });

  it("goes by what the server says, so a computer already connected skips the install step even if hosting was set up long ago", () => {
    expect(wizardSteps(input({ isProvider: true, machines: [M({ asking_price_per_hour: null })] }))[2].status).toBe("done");
  });
});

describe("rentalLine", () => {
  it("says nothing unless the host wants rental and a computer exists", () => {
    expect(rentalLine([], true)).toBeNull();
    expect(rentalLine([M()], false)).toBeNull();
  });
  it("tells on, too-old agent, and not-yet apart", () => {
    expect(rentalLine([M({ capabilities: ["container_tier", "container_rental"] })], true)).toBe("Container rental is on for this computer.");
    expect(rentalLine([M({ agent_version: "0.1.0", capabilities: [] })], true)).toMatch(/too old for container rental/);
    expect(rentalLine([M({ capabilities: ["container_tier"] })], true)).toMatch(/not on yet/);
  });
});

import { PREFS_KEY, readPrefs, writePrefs } from "./host-setup";

describe("remembering the choice", () => {
  const memory = () => {
    const data = new Map<string, string>();
    return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v), data };
  };
  it("saves and reads back the choice", () => {
    const s = memory();
    writePrefs({ persona: "many", mode: "advanced" }, s);
    expect(readPrefs(s)).toEqual({ persona: "many", mode: "advanced" });
    expect(Object.keys(Object.fromEntries(s.data))).toEqual([PREFS_KEY]);
  });
  it("saves nothing but the two words (no token, no address)", () => {
    const s = memory();
    writePrefs({ persona: "one_pc", mode: "easy" }, s);
    expect(JSON.parse(s.data.get(PREFS_KEY) as string)).toEqual({ persona: "one_pc", mode: "easy" });
  });
  it("gives null for nothing saved, broken text and unknown values", () => {
    expect(readPrefs(memory())).toBeNull();
    for (const bad of ["{", "null", "42", '"easy"', "{}", '{"persona":"one_pc"}', '{"persona":"x","mode":"easy"}', '{"persona":"one_pc","mode":"x"}']) {
      const s = memory();
      s.setItem(PREFS_KEY, bad);
      expect(readPrefs(s), bad).toBeNull();
    }
  });
  it("never throws when storage is missing or refuses", () => {
    const refusing = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); } };
    expect(readPrefs(null)).toBeNull();
    expect(readPrefs(refusing)).toBeNull();
    expect(() => writePrefs({ persona: "server", mode: "advanced" }, null)).not.toThrow();
    expect(() => writePrefs({ persona: "server", mode: "advanced" }, refusing)).not.toThrow();
  });
});
