"use client";

import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { CopyField } from "@/components/provider/copy-field";
import {
  WINDOWS_CONSENT_POINTS,
  WINDOWS_INSTALL_NEEDS,
  WINDOWS_RENTAL_CONSENT_POINTS,
  buildWindowsInstallCommand,
  isWindowsInstallerPublished,
} from "@/lib/windows-install-command";

/**
 * Step 3c for a Windows PC (EXPERIMENTAL). It shows what the PC needs and what the installer does, asks the person to read it, and only then shows one
 * PowerShell block. While the installer has no published address and checksum there is NOTHING to copy, and the panel says so plainly.
 */
export function WindowsInstallPanel({
  msiUrl,
  sha256,
  server,
  token,
  allowRental,
  onAllowRentalChange,
}: {
  msiUrl?: string | null;
  sha256?: string | null;
  server: string | null;
  token?: string | null;
  allowRental: boolean;
  onAllowRentalChange: (value: boolean) => void;
}) {
  const [consented, setConsented] = useState(false);
  const published = isWindowsInstallerPublished({ msiUrl, sha256 });
  const prompt = consented ? buildWindowsInstallCommand({ msiUrl, sha256, server, allowRental }) : null;
  const withToken = consented && token ? buildWindowsInstallCommand({ msiUrl, sha256, server, allowRental, token }) : null;
  return (
    <div className="space-y-3" data-testid="windows-install">
      <h3 className="text-sm font-semibold">3c. Install and connect (Windows 11, experimental)</h3>
      <p className="flex items-start gap-2 rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-sm">
        <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
        <span>
          <span className="font-medium">Experimental.</span> Sharing a Windows PC has not been proven on a real PC yet. Linux is the recommended choice.
        </span>
      </p>
      <div className="space-y-1.5 text-sm">
        <p className="font-medium">What the PC needs</p>
        <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
          {WINDOWS_INSTALL_NEEDS.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </div>

      {!published ? (
        <p role="status" className="text-sm text-muted-foreground" data-testid="windows-not-published">
          The Windows installer has not been published yet, so there is nothing to install here. Use a Linux computer for now, or ask the ComputeCoin team when it will be
          ready.
        </p>
      ) : (
        <>
          <details className="rounded-md border border-border px-4 py-3 text-sm" open>
            <summary className="cursor-pointer font-medium">Read this first: what the installer does and what you share</summary>
            <ul className="mt-2 list-disc space-y-1.5 pl-5 text-muted-foreground">
              {WINDOWS_CONSENT_POINTS.map((t) => (
                <li key={t}>{t}</li>
              ))}
              {allowRental
                ? WINDOWS_RENTAL_CONSENT_POINTS.map((t) => (
                    <li key={t} data-testid="windows-rental-point">
                      {t}
                    </li>
                  ))
                : null}
            </ul>
          </details>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={allowRental} onChange={(e) => onAllowRentalChange(e.target.checked)} />
            <span className="font-medium">Also let customers run their own code on this computer (container rental)</span>
          </label>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={consented} onChange={(e) => setConsented(e.target.checked)} />
            <span className="font-medium">I have read this, and I am allowed to share this computer.</span>
          </label>
          {!consented ? (
            <p className="text-xs text-muted-foreground">Tick the box above to see the command.</p>
          ) : prompt ? (
            <div className="space-y-2">
              <CopyField label="Install and connect (Windows, in an administrator PowerShell)" value={prompt} />
              <p className="text-xs text-muted-foreground">
                It asks for your setup token with your typing hidden: paste the token you created in step 3b. When it finishes, open{" "}
                <strong className="font-medium text-foreground">My computers</strong>: the first start sets up the Linux system and can take several minutes.
              </p>
              {withToken ? (
                <CopyField label="The same, with your token already in it (do not share it)" value={withToken} />
              ) : null}
            </div>
          ) : (
            <p role="status" className="text-sm text-muted-foreground">
              The command could not be built from this website&apos;s address. Ask the ComputeCoin team.
            </p>
          )}
        </>
      )}
    </div>
  );
}
