import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { Button } from "@/components/ui/button";

// Static content: no data, no client code. Cross-platform plan W14. The numbers below are PLACEHOLDERS that mirror
// provider-agent/computecoin_agent/windows/runtime/requirements_check.py until the owner fixes the minimum Windows and
// NVIDIA driver versions (plan section 9); change both together. Accessibility rules from B7: one h1, headings in order,
// a captioned table with header cells, descriptive link text, no information carried by colour alone.

export const metadata: Metadata = {
  title: "Share a Windows PC",
  description:
    "What a Windows PC needs to share its NVIDIA graphics card on ComputeCoin, what the requirements check looks at, and what Windows hosts cannot do.",
};

const CHECKS: ReadonlyArray<{ name: string; meaning: string; fix: string }> = [
  {
    name: "Processor type",
    meaning: "A 64-bit Intel or AMD processor. Windows on ARM is not supported yet.",
    fix: "Nothing to fix: this PC cannot take part.",
  },
  {
    name: "Windows version",
    meaning: "A version of Windows 10 or 11 recent enough to give the graphics card to Linux programs (WSL 2).",
    fix: "Install the latest Windows updates, or upgrade to Windows 11.",
  },
  {
    name: "Virtualization",
    meaning: "A feature of your processor that lets Windows run a small Linux system. It is switched on in the PC's firmware setup (BIOS or UEFI).",
    fix: "Restart, open the firmware settings, turn on the virtualization option (named Intel Virtualization Technology or SVM Mode, depending on the maker), save and restart.",
  },
  {
    name: "Windows Subsystem for Linux (WSL 2)",
    meaning: "The Windows feature that runs the separate Linux system the jobs use.",
    fix: "In a PowerShell window opened as administrator, run wsl --install (new) or wsl --update (old), then restart.",
  },
  {
    name: "NVIDIA graphics driver",
    meaning: "A current driver for your NVIDIA card, installed on Windows. You do not install anything NVIDIA inside the Linux system.",
    fix: "Download the latest driver for your card from nvidia.com or the GeForce app, install it and restart.",
  },
  {
    name: "Free disk space",
    meaning: "Room for the Linux system and the jobs' files. We suggest at least 60 GB free on the drive you choose.",
    fix: "Free up space, or choose another drive.",
  },
  {
    name: "Memory (RAM)",
    meaning: "At least 8 GB, 16 GB or more is better.",
    fix: "Close other programs, or share a computer with more memory.",
  },
];

export default function ShareWindowsPage() {
  return (
    <>
      <PageHero
        eyebrow="Become a host"
        title="Share a Windows PC"
        description="You can let other people use your NVIDIA graphics card while you are not using it. Here is what the PC needs, what the requirements check looks at, and what Windows computers cannot do."
      />
      <div className="container space-y-8 py-12">
        <div role="note" className="max-w-3xl rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
          <p className="font-medium">Not available to install yet</p>
          <p className="mt-1 text-muted-foreground">
            Windows sharing is being built and has not been tested on real Windows computers. There is no
            installer yet. This page explains how it is planned to work, so you know early whether your PC
            would qualify. Nothing here is a promise of a date, or of earnings. Today the preview works on Linux only.
          </p>
        </div>

        <Prose>
          <h2>What you need</h2>
          <ul>
            <li>
              <strong>A Windows 10 or Windows 11 PC</strong> with a 64-bit Intel or AMD processor and
              virtualization switched on in the firmware setup.
            </li>
            <li>
              <strong>An NVIDIA graphics card (GPU)</strong> with at least 8 GB of video memory, and a
              current NVIDIA driver for Windows. AMD, Intel and Apple graphics cards are not supported on
              Windows.
            </li>
            <li>
              <strong>At least 8 GB of memory</strong> (16 GB or more is better) and about 60 GB of free
              disk space.
            </li>
            <li>
              <strong>An internet connection.</strong> The program only connects out. You do not open any
              ports on your router.
            </li>
            <li>
              <strong>Administrator rights</strong> on the PC to install it.
            </li>
          </ul>

          <h2>What Windows PCs can and cannot do</h2>
          <ul>
            <li>
              <strong>Ordinary jobs only.</strong> Windows PCs only take jobs in the standard locked
              container. They do not take private jobs, bring-your-own-container jobs, or jobs that need
              several computers working together. We cannot promise a customer that you could not see their
              data, so those jobs are not sent to Windows PCs.
            </li>
            <li>
              <strong>NVIDIA only.</strong> AMD graphics cards are only supported on Linux for now.
            </li>
            <li>
              <strong>Your PC comes first.</strong> By default the program does not start new jobs while you
              are using the graphics card, while you are active at the keyboard or mouse, while a
              full-screen program is open, or while the PC runs on battery. It finishes a job it already
              started.
            </li>
            <li>
              <strong>Test credits.</strong> While ComputeCoin is a preview, what you earn is shown in test
              credits, not real money. We cannot promise any amount of work or earnings.
            </li>
          </ul>

          <h2>How it is kept away from your files</h2>
          <p>
            Customer jobs do not run directly on Windows. They run inside a separate, small Linux system
            that the program sets up just for this, and inside a locked container within it. That system
            cannot see your Windows drives, your Windows programs or the other computers in your home.
            Jobs get no internet unless a job is allowed a short list of sites. A container is strong
            protection but not a perfect wall, so use a PC that holds nothing you could not afford to lose.
          </p>

          <h2 id="requirements-check">What the requirements check looks at</h2>
          <p>
            Before anything is installed the program runs a check. It only reads information about your PC:
            it changes nothing. It gives a plain list of passes and fails with what to do about each. If it
            cannot tell something, it says so and does not let you continue until that is resolved.
          </p>
        </Prose>

        <div className="max-w-4xl overflow-x-auto" tabIndex={0} role="region" aria-label="Requirements check, scrollable table">
          <Prose className="max-w-none">
            <table>
              <caption className="pb-2 text-left text-sm text-muted-foreground">
                The seven things the requirements check looks at, what each means and how to fix a fail.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">What it means</th>
                  <th scope="col">If it fails</th>
                </tr>
              </thead>
              <tbody>
                {CHECKS.map((c) => (
                  <tr key={c.name}>
                    <th scope="row" className="pr-4 text-left align-top text-sm font-medium normal-case tracking-normal text-foreground">{c.name}</th>
                    <td>{c.meaning}</td>
                    <td>{c.fix}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Prose>
        </div>

        <Prose>
          <p>
            Each line in the result is marked with a word (&ldquo;passed&rdquo;, &ldquo;failed&rdquo;,
            &ldquo;warning&rdquo; or &ldquo;could not tell&rdquo;), never with a colour alone. The exact
            version numbers are still being decided and will be shown here when they are.
          </p>

          <h2>Stop and remove</h2>
          <p>
            You can pause sharing at any time from the host page, or stop the program. Uninstalling it is
            meant to remove the program, the separate Linux system, its disk file, the firewall rules and
            the stored login, and to leave nothing behind.
          </p>
        </Prose>

        <div className="flex flex-wrap gap-3">
          <Button asChild>
            <Link href="/providers">What every host needs to know</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/help#host">Help for hosts</Link>
          </Button>
        </div>
      </div>
    </>
  );
}
