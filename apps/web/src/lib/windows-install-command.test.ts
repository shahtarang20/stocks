import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  WINDOWS_CONSENT_POINTS,
  WINDOWS_INSTALL_NEEDS,
  WINDOWS_RENTAL_CONSENT_POINTS,
  buildWindowsInstallCommand,
  isWindowsInstallerPublished,
} from "./windows-install-command";

const repo = (rel: string) => readFileSync(fileURLToPath(new URL(`../../../../${rel}`, import.meta.url)), "utf8");
const URL_OK = "https://github.com/acme/stocks/releases/download/agent-v0.2.1/ComputeCoinAgent-0.2.1.msi";
const SHA = "ABCDEF0123456789abcdef0123456789ABCDEF0123456789abcdef0123456789";
const SERVER = "https://api.example.com";
const TOKEN = "enr_FAKE-TEST_FAKE-TEST_FAKE-TEST_FAKE-TEST"; // gitleaks:allow
const base = { msiUrl: URL_OK, sha256: SHA, server: SERVER };

describe("buildWindowsInstallCommand", () => {
  it("builds nothing until the installer's address and checksum are both known", () => {
    for (const bad of [{}, { msiUrl: URL_OK }, { sha256: SHA }, { msiUrl: null, sha256: null }, { msiUrl: "", sha256: SHA }]) {
      expect(buildWindowsInstallCommand({ ...bad, server: SERVER })).toBeNull();
      expect(isWindowsInstallerPublished(bad)).toBe(false);
    }
    expect(isWindowsInstallerPublished({ msiUrl: URL_OK, sha256: SHA })).toBe(true);
  });

  it("downloads, checks the checksum before installing, and removes the file", () => {
    const text = buildWindowsInstallCommand(base) as string;
    const order = ["Invoke-WebRequest", "Get-FileHash", "Start-Process msiexec.exe", "service start"].map((s) => text.indexOf(s));
    expect(order.every((i) => i >= 0) && [...order].sort((a, b) => a - b).join() === order.join()).toBe(true);
    expect(text).toContain(`-ne '${SHA.toLowerCase()}'`);
    expect(text).toContain("Nothing was installed");
    expect(text).toContain("LicenseAccepted=1");
    expect(text.match(/Remove-Item -LiteralPath \$msi -Force/g)?.length).toBe(2); // on a wrong checksum, and after installing
  });

  it("stops if the installer fails, before it tries to connect anything", () => {
    const text = buildWindowsInstallCommand(base) as string;
    expect(text.indexOf("if ($p.ExitCode -ne 0) { throw")).toBeLessThan(text.indexOf("enroll --server"));
  });

  it("asks for the token with typing hidden when none is given, and never prints it", () => {
    const text = buildWindowsInstallCommand(base) as string;
    expect(text).toContain("Read-Host 'Paste your setup token (your typing is hidden)' -AsSecureString");
    expect(text).toContain("ZeroFreeBSTR");
    expect(text).not.toContain("enr_");
  });

  it("puts a given token in a temporary file and deletes it afterwards, and never on the enroll command line", () => {
    const text = buildWindowsInstallCommand({ ...base, token: TOKEN }) as string;
    expect(text).toContain(`Set-Content -LiteralPath $tf -Value '${TOKEN}' -NoNewline -Encoding ascii`);
    expect(text).toContain("enroll --server 'https://api.example.com' --token-file $tf");
    expect(text).toMatch(/finally \{ Remove-Item -LiteralPath \$tf -Force \}/);
    const enrollLine = text.split("\n").find((l) => l.includes("enroll --server")) as string;
    expect(enrollLine).not.toContain("enr_");
    expect(text.split(TOKEN).length - 1).toBe(1); // exactly once
  });

  it("adds the rental test and switch only when asked, before the service starts", () => {
    const plain = buildWindowsInstallCommand(base) as string;
    expect(plain).not.toContain("rental");
    const text = buildWindowsInstallCommand({ ...base, allowRental: true }) as string;
    expect(text.indexOf("rental check")).toBeLessThan(text.indexOf("rental on"));
    expect(text.indexOf("rental on")).toBeLessThan(text.indexOf("service start"));
    expect(text.indexOf("enroll --server")).toBeLessThan(text.indexOf("rental check"));
    expect(text).toContain("customers cannot rent it until the problem is fixed"); // a failing test never stops the install
    expect(buildWindowsInstallCommand({ ...base, allowRental: false })).not.toContain("rental");
  });

  it("uses the bundled Python by its full path, isolated, with no shell and no PATH lookup", () => {
    const text = buildWindowsInstallCommand(base) as string;
    expect(text).toContain("Join-Path $env:ProgramFiles 'ComputeCoin\\python\\python.exe'");
    expect(text.match(/& \$py -I -m computecoin_agent/g)?.length).toBeGreaterThanOrEqual(2);
    expect(text).not.toMatch(/Invoke-Expression|iex\b|cmd \/c|\| powershell/i);
  });

  it("never builds from anything that is not a plain value", () => {
    const bad = [
      { msiUrl: "http://example.com/a.msi" }, // not https
      { msiUrl: "https://example.com/a.exe" }, // not an msi
      { msiUrl: "https://example.com/a'; rm x; '.msi" },
      { msiUrl: "https://example.com/a b.msi" },
      { msiUrl: "https://example.com/a.msi?x=1" },
      { sha256: "abc" },
      { sha256: SHA + "0" },
      { sha256: SHA.replace("A", "'") },
      { server: "ftp://x.com" },
      { server: "https://x.com;id" },
      { server: "http://evil.example.com" },
      { token: "enr_" },
      { token: "enr_abc'; Remove-Item *; '12345678" },
      { token: "enr_abcdefgh 12345678" },
      { token: "not-a-token" },
    ];
    for (const over of bad) expect(buildWindowsInstallCommand({ ...base, ...over }), JSON.stringify(over)).toBeNull();
  });

  it("allows the local server only for this computer and strips trailing slashes", () => {
    expect(buildWindowsInstallCommand({ ...base, server: "http://localhost:8000" })).toContain("--server 'http://localhost:8000'");
    expect(buildWindowsInstallCommand({ ...base, server: "https://api.example.com//" })).toContain("--server 'https://api.example.com'");
  });
});

describe("the Windows install text matches the installer and the code", () => {
  it("lists the requirements the code really checks", () => {
    const needs = WINDOWS_INSTALL_NEEDS.join(" ");
    expect(needs).toMatch(/Windows 11 \(build 22000/);
    expect(repo("provider-agent/computecoin_agent/windows/runtime/requirements_check.py")).toMatch(/min_windows_build: int = 22000/);
    expect(needs).toMatch(/535\.86/);
    expect(needs).toMatch(/TCC mode/);
    expect(needs).toMatch(/Run as administrator/);
  });

  it("names the program folder, the service and the way to remove it, as the installer's consent text does", () => {
    const consent = repo("provider-agent/windows/installer/consent.txt");
    const points = WINDOWS_CONSENT_POINTS.join(" ");
    expect(points).toContain("ComputeCoin Agent");
    expect(consent).toContain('"ComputeCoin Agent"');
    expect(points).toMatch(/outbound connections only/);
    expect(consent).toMatch(/OUTBOUND connections only/);
    expect(points).toMatch(/isolated Linux system \(WSL2\)/);
    expect(consent).toMatch(/WSL2 distro named "computecoin"/);
    expect(points).toMatch(/Uninstall removes the service|Uninstall removes/);
    expect(consent).toMatch(/Uninstall removes the service/);
    expect(points).toMatch(/asks Windows not to sleep/);
    expect(consent).toMatch(/asks Windows not to put the PC to sleep/);
  });

  it("says that rental needs the host's own opt-in, that the host can see the customer's files, and that Windows gives all its cards", () => {
    const text = WINDOWS_RENTAL_CONSENT_POINTS.join(" ");
    expect(text).toMatch(/can see the customer's files/);
    expect(text).toMatch(/all of the PC's graphics cards/);
    expect(text).toMatch(/restart or hibernate/);
  });

  it("calls only commands the agent has, and the service start the Windows service has", () => {
    const cli = repo("provider-agent/computecoin_agent/cli.py");
    expect(cli).toContain("--token-file");
    expect(cli).toMatch(/action not in \(([^)]*"check"[^)]*"on"|[^)]*"on"[^)]*"check")/);
    expect(repo("provider-agent/computecoin_agent/windows/cli_commands.py")).toMatch(/def start/);
    expect(repo("provider-agent/computecoin_agent/__main__.py")).toBeTruthy(); // `python -m computecoin_agent` exists
  });
});
