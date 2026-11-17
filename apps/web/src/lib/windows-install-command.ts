/**
 * The Windows install block shown on the host setup page (EXPERIMENTAL). One PowerShell block, run in an ADMINISTRATOR window, that downloads the
 * installer (an MSI), refuses it unless its SHA-256 matches, installs it silently with the consent flag, connects the computer with a setup token,
 * optionally turns container rental on, and starts the service. It is built only from plain values the page can vouch for, like the Linux line.
 *
 * Nothing is built until the installer's address AND its SHA-256 are known: until the installer is published there is nothing to copy.
 * The consent flag (`LicenseAccepted=1`) is the same one the installer demands: the page asks the person to read the consent text first.
 */

const HEX64_RE = /^[0-9a-fA-F]{64}$/;
// https only (a download that carries software must not travel unencrypted), a file ending in .msi, and nothing that could break out of a quote.
const MSI_URL_RE = /^https:\/\/[A-Za-z0-9._-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~/-]*)?\/[A-Za-z0-9._-]+\.msi$/;
const SERVER_RE = /^https:\/\/[A-Za-z0-9._-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~/-]*)?$/;
const LOCAL_SERVER_RE = /^http:\/\/(localhost|127\.0\.0\.1)(:[0-9]{1,5})?(\/[A-Za-z0-9._~/-]*)?$/;
const TOKEN_RE = /^enr_[A-Za-z0-9_-]{8,200}$/;

export interface WindowsInstallInput {
  /** Where the installer (an .msi) can be downloaded: https only. */
  msiUrl?: string | null;
  /** The installer's SHA-256 (64 hex characters). */
  sha256?: string | null;
  /** The API address the computer connects to. */
  server?: string | null;
  /** A one-time setup token. When given it is part of the block; when not, the block asks for it with typing hidden. */
  token?: string | null;
  /** Also run `rental check` and `rental on` (container rental). Off unless the host ticks the box. */
  allowRental?: boolean;
}

export function isWindowsInstallerPublished(input: WindowsInstallInput): boolean {
  return typeof input.msiUrl === "string" && MSI_URL_RE.test(input.msiUrl) && typeof input.sha256 === "string" && HEX64_RE.test(input.sha256);
}

/** The block of PowerShell lines, or null when anything it needs is missing or not a plain value. Never contains a quote-breaking character. */
export function buildWindowsInstallCommand(input: WindowsInstallInput): string | null {
  if (!isWindowsInstallerPublished(input)) return null;
  const server = typeof input.server === "string" ? input.server.replace(/\/+$/, "") : "";
  if (!(SERVER_RE.test(server) || LOCAL_SERVER_RE.test(server))) return null;
  const hasToken = input.token !== undefined && input.token !== null && input.token !== "";
  if (hasToken && !(typeof input.token === "string" && TOKEN_RE.test(input.token))) return null;

  const tokenLines = hasToken
    ? [`$tf = New-TemporaryFile`, `Set-Content -LiteralPath $tf -Value '${input.token}' -NoNewline -Encoding ascii`]
    : [
        `$secure = Read-Host 'Paste your setup token (your typing is hidden)' -AsSecureString`,
        `$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)`,
        `$tf = New-TemporaryFile`,
        `try { Set-Content -LiteralPath $tf -Value ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)) -NoNewline -Encoding ascii } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }`,
      ];
  const rentalLines =
    input.allowRental === true
      ? [
          `& $py -I -m computecoin_agent rental check`,
          `if ($LASTEXITCODE -ne 0) { Write-Warning 'The rental test found a problem. Read the lines marked FAIL above. The computer is connected and still works for ordinary jobs.' }`,
          `& $py -I -m computecoin_agent rental on`,
        ]
      : [];
  return [
    `$ErrorActionPreference = 'Stop'`,
    `$msi = Join-Path $env:TEMP 'ComputeCoinAgent.msi'`,
    `Invoke-WebRequest -Uri '${input.msiUrl}' -OutFile $msi`,
    `if ((Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash -ne '${(input.sha256 as string).toLowerCase()}') { Remove-Item -LiteralPath $msi -Force; throw 'The download does not match its checksum. Nothing was installed.' }`,
    `$p = Start-Process msiexec.exe -ArgumentList '/i', ('"' + $msi + '"'), '/qn', 'LicenseAccepted=1' -Wait -PassThru`,
    `Remove-Item -LiteralPath $msi -Force`,
    `if ($p.ExitCode -ne 0) { throw "The installer stopped (exit code $($p.ExitCode)). Nothing was changed." }`,
    `$py = Join-Path $env:ProgramFiles 'ComputeCoin\\python\\python.exe'`,
    ...tokenLines,
    `try { & $py -I -m computecoin_agent enroll --server '${server}' --token-file $tf } finally { Remove-Item -LiteralPath $tf -Force }`,
    ...rentalLines,
    `& $py -I -m computecoin_agent service start`,
  ].join("\n");
}

/** What a Windows host needs before installing. */
export const WINDOWS_INSTALL_NEEDS: string[] = [
  "Windows 11 (build 22000 or newer). Windows 10 cannot run the Linux system the agent uses.",
  "Virtualization switched on in the BIOS.",
  "A GeForce or RTX card in its normal mode, and an NVIDIA driver for Windows (535.86 or newer). AMD and Intel cards, data-centre cards and cards in TCC mode do not work.",
  "A PowerShell window opened with \"Run as administrator\".",
];

/** The plain points the installer's consent screen covers (installer/consent.txt is the full text), so a host reads them before the command is shown. */
export const WINDOWS_CONSENT_POINTS: string[] = [
  "It installs a Windows service called \"ComputeCoin Agent\" that starts with Windows, a small bundled Python, and a data folder that only administrators can open.",
  "It adds a firewall rule that allows outbound connections only. The agent opens no port that other computers can reach.",
  "It shares your graphics card, some processor, memory and disk while a job runs, and basic facts about the PC (Windows version, card model, free resources). No files, documents, browsing data or passwords.",
  "Customers' work runs only inside an isolated Linux system (WSL2), never directly on Windows, and cannot see your Windows files or drives.",
  "By default it stays out of your way: not while you use the card, not on battery, and never during a full-screen game. While a job runs it asks Windows not to sleep.",
  "You can pause sharing at any time, and Settings, Apps, ComputeCoin Agent, Uninstall removes the service, the Linux system, the firewall rule, your stored credential and the data folder.",
];

/** Extra points when the host ticks "allow container rental". */
export const WINDOWS_RENTAL_CONSENT_POINTS: string[] = [
  "Customers may run their OWN code in a locked container in that Linux system. It is not a private environment, so use a PC with nothing private on it. You can see the customer's files while it runs.",
  "On Windows each rental gets one graphics card, and a restart or hibernate ends a rental.",
];
