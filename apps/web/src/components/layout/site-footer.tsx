import Link from "next/link";
import { Logo } from "./logo";

const COLUMNS: { title: string; links: { href: string; label: string }[] }[] = [
  {
    title: "Product",
    links: [
      { href: "/marketplace/gpus", label: "Computers for rent" },
      { href: "/pricing", label: "Pricing" },
      { href: "/status", label: "Service status" },
    ],
  },
  {
    title: "Get started",
    links: [
      { href: "/providers", label: "Become a host" },
      { href: "/dashboard", label: "My dashboard" },
      { href: "/developers", label: "For developers" },
      { href: "/help", label: "Help and glossary" },
    ],
  },
  {
    title: "Company",
    links: [
      { href: "/about", label: "About" },
      { href: "/security", label: "Security" },
      { href: "/whitepaper", label: "Whitepaper" },
      { href: "/legal/terms", label: "Terms" },
      { href: "/legal/privacy", label: "Privacy" },
      { href: "/legal/aup", label: "Acceptable use policy" },
    ],
  },
];

export function SiteFooter() {
  return (
    <footer className="border-t border-border bg-surface/40">
      <div className="container grid grid-cols-2 gap-8 py-10 sm:grid-cols-3 sm:py-12 md:grid-cols-[1.4fr_repeat(3,1fr)] md:gap-10">
        <div className="col-span-2 space-y-3 sm:col-span-3 md:col-span-1">
          <Logo />
          <p className="max-w-xs text-sm text-muted-foreground">
            A marketplace where people rent graphics cards (GPUs) from other
            people. This is a preview. Credits are test credits, not real money.
          </p>
        </div>
        {COLUMNS.map((col) => (
          <div key={col.title} className="space-y-3">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-foreground">
              {col.title}
            </h2>
            <ul className="space-y-2">
              {col.links.map((link) => (
                <li key={link.href}>
                  <Link
                    href={link.href}
                    className="rounded-sm text-sm text-muted-foreground transition-colors hover:text-foreground hover:underline underline-offset-4"
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
          <p>© {new Date().getFullYear()} ComputeCoin. Preview version.</p>
          <p>
            This site never shows a coin price or trading numbers.
          </p>
        </div>
      </div>
    </footer>
  );
}
