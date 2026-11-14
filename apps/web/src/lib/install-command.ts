/**
 * The one-line install command shown on the host setup page. It must be accepted by `public/install.sh`, which refuses
 * anything that is not plain, so the same rules are applied here and a value that would be refused is never put in the
 * command: a host copies exactly what is shown.
 */

const TOKEN_RE = /^enr_[A-Za-z0-9_-]{8,200}$/;
// https anywhere; plain http only for this computer itself (a setup token must not travel unencrypted). Same rule as install.sh.
const HTTPS_RE = /^https:\/\/[A-Za-z0-9._-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~/-]*)?$/;
const LOCAL_HTTP_RE = /^http:\/\/(localhost|127\.0\.0\.1)(:[0-9]{1,5})?(\/[A-Za-z0-9._~/-]*)?$/;

export const TOKEN_PLACEHOLDER = "PASTE_YOUR_TOKEN_HERE";

export function isPlainToken(value: string | null | undefined): value is string {
  return typeof value === "string" && TOKEN_RE.test(value);
}

export function isPlainUrl(value: string | null | undefined): value is string {
  return typeof value === "string" && (HTTPS_RE.test(value) || LOCAL_HTTP_RE.test(value));
}

function trimSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

export interface InstallCommandInput {
  /** The address of this website, for example https://app.example.com. */
  origin: string | null | undefined;
  /** The API address hosts connect to. */
  server: string | null | undefined;
  /** The one-time setup token, once it has been created. Only used by the unattended command. */
  token?: string | null;
  /** Where the agent package is published, when it is not the project's default release page. */
  packageBase?: string | null;
}

function baseParts(input: InstallCommandInput): { origin: string; server: string; extra: string } | null {
  const origin = input.origin ? trimSlash(input.origin) : "";
  const server = input.server ? trimSlash(input.server) : "";
  if (!isPlainUrl(origin) || !isPlainUrl(server)) return null;
  const base = input.packageBase ? trimSlash(input.packageBase) : "";
  if (base && !isPlainUrl(base)) return null;
  return { origin, server, extra: base ? ` --package-base ${base}` : "" };
}

/**
 * The line a host pastes. It does NOT contain the token: the installer asks for it (typing hidden), so the token is never
 * on a command line that other users of the computer could read. The script is saved to a temporary file and then run, not
 * piped, so the installer keeps the host's real terminal for that question under every version of sudo. Null when it cannot
 * be built honestly.
 */
export function buildInstallCommand(input: InstallCommandInput): string | null {
  const p = baseParts(input);
  return p
    ? `f=$(mktemp) && curl -fsSL ${p.origin}/install.sh -o "$f" && sudo sh "$f" --server ${p.server}${p.extra}; rm -f "$f"`
    : null;
}

/**
 * For scripts and automation that cannot answer a prompt: the token is part of the command (so it is visible to other users
 * of that computer while the install runs). Without a token the placeholder is used, and the installer says clearly that it
 * is not one.
 */
export function buildUnattendedInstallCommand(input: InstallCommandInput): string | null {
  const p = baseParts(input);
  if (!p) return null;
  const token = isPlainToken(input.token) ? input.token : TOKEN_PLACEHOLDER;
  return `curl -fsSL ${p.origin}/install.sh | sudo sh -s -- --token ${token} --server ${p.server}${p.extra}`;
}

export function uninstallCommand(origin: string | null | undefined): string | null {
  const o = origin ? trimSlash(origin) : "";
  return isPlainUrl(o) ? `curl -fsSL ${o}/install.sh | sudo sh -s -- --uninstall` : null;
}
