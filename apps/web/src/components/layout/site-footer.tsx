import Link from "next/link";
import { Logo } from "./logo";

const COLUMNS: { title: string; links: { href: string; label: string }[] }[] = [
  {
    title: "Product",
    links: [
      { href: "/marketplace", label: "Marketplace" },
      { href: "/marketplace/gpus", label: "GPU listings" },
      { href: "/pricing", label: "Pricing" },
      { href: "/status", label: "Status" },
    ],
  },
  {
    title: "Build",
    links: [
      { href: "/developers", label: "Developers" },
      { href: "/providers", label: "Become a provider" },
      { href: "/security", label: "Security" },
      { href: "/dashboard", label: "Console" },
    ],
  },
  {
    title: "Company",
    links: [
      { href: "/about", label: "About" },
      { href: "/whitepaper", label: "Whitepaper" },
      { href: "/legal/terms", label: "Terms" },
      { href: "/legal/privacy", label: "Privacy" },
    ],
  },
];

export function SiteFooter() {
  return (
    <footer className="border-t border-border bg-surface/40">
      <div className="container grid gap-10 py-12 md:grid-cols-[1.4fr_repeat(3,1fr)]">
        <div className="space-y-3">
          <Logo />
          <p className="max-w-xs text-sm text-muted-foreground">
            A decentralized marketplace for AI GPU compute. Phase 1 developer
            preview — measured, not promised.
          </p>
        </div>
        {COLUMNS.map((col) => (
          <div key={col.title} className="space-y-3">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {col.title}
            </h4>
            <ul className="space-y-2">
              {col.links.map((link) => (
                <li key={link.href}>
                  <Link
                    href={link.href}
                    className="text-sm text-muted-foreground transition-colors hover:text-foreground"
                  >
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <div className="border-t border-border">
        <div className="container flex flex-col items-start justify-between gap-2 py-6 text-xs text-muted-foreground sm:flex-row sm:items-center">
          <p>© {new Date().getFullYear()} ComputeCoin. Phase 1 preview.</p>
          <p>
            No token price, market cap, or trading metrics are shown anywhere on
            this site.
          </p>
        </div>
      </div>
    </footer>
  );
}
