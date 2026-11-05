"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, X } from "lucide-react";
import { Logo } from "./logo";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/marketplace", label: "Rent a GPU" },
  { href: "/pricing", label: "Pricing" },
  { href: "/developers", label: "Developers" },
  { href: "/providers", label: "Become a host" },
  { href: "/help", label: "Help" },
  { href: "/security", label: "Security" },
  { href: "/status", label: "Status" },
];

const linkBase =
  "rounded-md px-3 py-2 text-sm transition-colors hover:bg-surface-2 hover:text-foreground";

export function SiteHeader() {
  const [open, setOpen] = useState(false);
  const { isAuthenticated } = useAuth();
  const pathname = usePathname() ?? "";
  const isCurrent = (href: string) =>
    pathname === href || pathname.startsWith(`${href}/`);

  // Close the mobile menu on navigation and on Escape.
  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur-md">
      <div className="container flex h-16 items-center justify-between gap-4">
        <div className="flex items-center gap-8">
          <Logo />
          <nav aria-label="Main" className="hidden items-center gap-1 lg:flex">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isCurrent(item.href) ? "page" : undefined}
                className={cn(
                  linkBase,
                  isCurrent(item.href)
                    ? "bg-surface-2 font-medium text-foreground"
                    : "text-muted-foreground",
                )}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </div>

        <div className="flex items-center gap-2">
          <ThemeToggle />
          <div className="hidden items-center gap-2 sm:flex">
            {isAuthenticated ? (
              <Button asChild size="sm">
                <Link href="/dashboard">Dashboard</Link>
              </Button>
            ) : (
              <>
                <Button asChild variant="ghost" size="sm">
                  <Link href="/login">Log in</Link>
                </Button>
                <Button asChild size="sm">
                  <Link href="/register">Create account</Link>
                </Button>
              </>
            )}
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden"
            aria-label="Toggle menu"
            aria-expanded={open}
            aria-controls="mobile-nav"
            onClick={() => setOpen((v) => !v)}
          >
            {open ? <X /> : <Menu />}
          </Button>
        </div>
      </div>

      <div
        id="mobile-nav"
        className={cn(
          "max-h-[calc(100dvh-4rem)] overflow-y-auto border-t border-border bg-background lg:hidden",
          open ? "block" : "hidden",
        )}
      >
        <nav aria-label="Mobile" className="container flex flex-col gap-1 py-4">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              onClick={() => setOpen(false)}
              aria-current={isCurrent(item.href) ? "page" : undefined}
              className={cn(
                linkBase,
                "py-2.5",
                isCurrent(item.href)
                  ? "bg-surface-2 font-medium text-foreground"
                  : "text-muted-foreground",
              )}
            >
              {item.label}
            </Link>
          ))}
          <div className="mt-3 flex gap-2 border-t border-border pt-4">
            {isAuthenticated ? (
              <Button asChild size="sm" className="flex-1">
                <Link href="/dashboard">Dashboard</Link>
              </Button>
            ) : (
              <>
                <Button asChild variant="outline" size="sm" className="flex-1">
                  <Link href="/login">Log in</Link>
                </Button>
                <Button asChild size="sm" className="flex-1">
                  <Link href="/register">Create account</Link>
                </Button>
              </>
            )}
          </div>
        </nav>
      </div>
    </header>
  );
}
