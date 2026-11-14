import { describe, expect, it } from "vitest";
import {
  buildInstallCommand,
  buildUnattendedInstallCommand,
  isPlainToken,
  isPlainUrl,
  TOKEN_PLACEHOLDER,
  uninstallCommand,
} from "./install-command";

const ORIGIN = "https://app.example.com";
const SERVER = "https://api.example.com";
const TOKEN = "enr_abcdefgh12345678XYZ";

describe("buildInstallCommand (the line a host pastes)", () => {
  it("never contains the token: the installer asks for it", () => {
    const cmd = buildInstallCommand({ origin: ORIGIN, server: SERVER, token: TOKEN });
    expect(cmd).toBe(`f=$(mktemp) && curl -fsSL ${ORIGIN}/install.sh -o "$f" && sudo sh "$f" --server ${SERVER}; rm -f "$f"`);
    expect(cmd).not.toContain("enr_");
    expect(cmd).not.toContain("--token");
  });

  it("saves the script and runs it from the file, so the installer has the host's terminal", () => {
    const cmd = buildInstallCommand({ origin: ORIGIN, server: SERVER }) as string;
    expect(cmd).toContain('sudo sh "$f"');
    expect(cmd).not.toContain("| sudo");  // not piped: a pipe would take the terminal away from the installer's question
    expect(cmd.endsWith('; rm -f "$f"')).toBe(true);  // and the temporary file is removed afterwards
  });

  it("strips trailing slashes", () => {
    expect(buildInstallCommand({ origin: `${ORIGIN}/`, server: `${SERVER}//` })).toBe(
      `f=$(mktemp) && curl -fsSL ${ORIGIN}/install.sh -o "$f" && sudo sh "$f" --server ${SERVER}; rm -f "$f"`,
    );
  });

  it("adds the package address only when one is set", () => {
    const cmd = buildInstallCommand({ origin: ORIGIN, server: SERVER, packageBase: "https://dl.example.com/agent/" });
    expect(cmd).toMatch(/ --package-base https:\/\/dl\.example\.com\/agent; rm -f "\$f"$/);
    expect(buildInstallCommand({ origin: ORIGIN, server: SERVER, packageBase: "" })).not.toContain("--package-base");
  });

  it("never builds a command the installer would refuse", () => {
    for (const bad of [null, undefined, "", "app.example.com", "ftp://x.com", "https://x.com/a b", "https://x.com;id", "javascript:alert(1)"]) {
      expect(buildInstallCommand({ origin: bad, server: SERVER })).toBeNull();
      expect(buildInstallCommand({ origin: ORIGIN, server: bad })).toBeNull();
    }
    expect(buildInstallCommand({ origin: ORIGIN, server: SERVER, packageBase: "https://x.com/a;b" })).toBeNull();
  });

  it("never points a host at an unencrypted address on another computer", () => {
    expect(buildInstallCommand({ origin: ORIGIN, server: "http://api.example.com" })).toBeNull();
    expect(buildInstallCommand({ origin: "http://app.example.com", server: SERVER })).toBeNull();
    expect(buildInstallCommand({ origin: ORIGIN, server: SERVER, packageBase: "http://dl.example.com/a" })).toBeNull();
    expect(isPlainUrl("http://127.0.0.1:8000")).toBe(true);
    expect(isPlainUrl("http://localhost.evil.com")).toBe(false);
  });

  it("works for a local test setup too", () => {
    expect(buildInstallCommand({ origin: "http://localhost:3000", server: "http://localhost:8000" })).toContain(
      "http://localhost:3000/install.sh",
    );
  });
});

describe("buildUnattendedInstallCommand (for scripts)", () => {
  it("has the token and server filled in", () => {
    expect(buildUnattendedInstallCommand({ origin: ORIGIN, server: SERVER, token: TOKEN })).toBe(
      `curl -fsSL ${ORIGIN}/install.sh | sudo sh -s -- --token ${TOKEN} --server ${SERVER}`,
    );
  });

  it("shows a placeholder, never an invented token, before one exists", () => {
    const cmd = buildUnattendedInstallCommand({ origin: ORIGIN, server: SERVER });
    expect(cmd).toContain(`--token ${TOKEN_PLACEHOLDER}`);
    expect(cmd).not.toMatch(/enr_/);
  });

  it("ignores a token that the installer would refuse", () => {
    for (const bad of ["enr_short", "abc", "enr_a b c d e f g h", "enr_$(id)12345678", "enr_a;b12345678"]) {
      expect(buildUnattendedInstallCommand({ origin: ORIGIN, server: SERVER, token: bad })).toContain(TOKEN_PLACEHOLDER);
    }
  });

  it("refuses the same bad addresses", () => {
    expect(buildUnattendedInstallCommand({ origin: "nope", server: SERVER, token: TOKEN })).toBeNull();
    expect(buildUnattendedInstallCommand({ origin: ORIGIN, server: SERVER, token: TOKEN, packageBase: "http://x.com/a" })).toBeNull();
  });
});

describe("the checks match the installer's", () => {
  it("accepts and refuses the same tokens and addresses", () => {
    expect(isPlainToken(TOKEN)).toBe(true);
    expect(isPlainToken("enr_abc")).toBe(false);
    expect(isPlainToken(null)).toBe(false);
    expect(isPlainUrl("https://api.x.com:8443/v1")).toBe(true);
    expect(isPlainUrl("https://x.com/$(id)")).toBe(false);
  });
});

describe("uninstallCommand", () => {
  it("is the same script with --uninstall", () => {
    expect(uninstallCommand(ORIGIN)).toBe(`curl -fsSL ${ORIGIN}/install.sh | sudo sh -s -- --uninstall`);
    expect(uninstallCommand("nope")).toBeNull();
    expect(uninstallCommand(null)).toBeNull();
  });
});
