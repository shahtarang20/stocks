"use client";

import { useEffect, useRef, useState, type ComponentType } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { LifeBuoy, Menu, X } from "lucide-react";
import { Logo } from "@/components/layout/logo";
import { ThemeToggle } from "@/components/theme-toggle";
import { UserMenu } from "./user-menu";
import { VerifyEmailBanner } from "./verify-email-banner";
import { ToastProvider } from "./toast";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface NavItem {
  href: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
}

export interface NavSection {
  title: string;
  items: NavItem[];
}

function NavLinks({
  sections,
  onNavigate,
}: {
  sections: NavSection[];
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  return (
    <nav aria-label="Console" className="flex flex-col gap-6 p-4">
      {sections.map((section) => (
        <div key={section.title} className="space-y-1">
          <p className="px-3 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {section.title}
          </p>
          {section.items.map((item) => {
            // Segment-aware prefix match so /dashboard/jobs/abc highlights Jobs but
            // /dashboard/jobs-archive (hypothetical) would not.
            const active =
              pathname === item.href ||
              (item.href !== "/dashboard" &&
                item.href !== "/provider/dashboard" &&
                pathname.startsWith(`${item.href}/`));
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                onClick={onNavigate}
                className={cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                  active
                    ? "bg-primary/10 font-medium text-primary"
                    : "text-muted-foreground hover:bg-surface-2 hover:text-foreground",
                )}
              >
                <item.icon aria-hidden="true" className="h-4 w-4" />
                {item.label}
              </Link>
            );
          })}
        </div>
      ))}
      <div className="space-y-1 border-t border-border pt-4">
        <Link
          href="/help"
          onClick={onNavigate}
          className="flex items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground"
        >
          <LifeBuoy aria-hidden="true" className="h-4 w-4" />
          Help and glossary
        </Link>
      </div>
    </nav>
  );
}

export function ConsoleShell({
  sections,
  children,
}: {
  sections: NavSection[];
  children: React.ReactNode;
}) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const pathname = usePathname();
  const toggleRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLDivElement>(null);

  // Close the drawer whenever the route changes (covers browser back/forward too).
  useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

  // While the drawer is open: Escape closes it and returns focus to the toggle,
  // the page behind does not scroll, and focus moves into the drawer.
  useEffect(() => {
    if (!mobileOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    drawerRef.current?.querySelector<HTMLElement>("a")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMobileOpen(false);
        toggleRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKey);
    };
  }, [mobileOpen]);

  return (
    <ToastProvider>
    <div className="flex min-h-dvh flex-col">
      {/* Top bar */}
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur">
        <div className="flex h-16 items-center justify-between gap-4 px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              ref={toggleRef}
              aria-label="Toggle navigation"
              aria-expanded={mobileOpen}
              aria-controls="console-mobile-nav"
              onClick={() => setMobileOpen((v) => !v)}
            >
              {mobileOpen ? <X /> : <Menu />}
            </Button>
            <Logo />
          </div>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <UserMenu />
          </div>
        </div>
      </header>

      <div className="flex flex-1">
        {/* Desktop sidebar */}
        <aside className="hidden w-64 shrink-0 border-r border-border lg:block">
          <div className="sticky top-16 max-h-[calc(100dvh-4rem)] overflow-y-auto">
            <NavLinks sections={sections} />
          </div>
        </aside>

        {/* Mobile sidebar overlay */}
        {mobileOpen ? (
          <div className="fixed inset-0 z-30 lg:hidden">
            <div
              aria-hidden="true"
              className="absolute inset-0 bg-black/50"
              onClick={() => setMobileOpen(false)}
            />
            <div
              id="console-mobile-nav"
              ref={drawerRef}
              className="absolute left-0 top-16 h-[calc(100dvh-4rem)] w-72 max-w-[85vw] overflow-y-auto border-r border-border bg-surface shadow-xl"
            >
              <NavLinks
                sections={sections}
                onNavigate={() => setMobileOpen(false)}
              />
            </div>
          </div>
        ) : null}

        <main id="main-content" tabIndex={-1} className="min-w-0 flex-1 bg-background focus:outline-none">
          <div className="mx-auto max-w-6xl px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
            <VerifyEmailBanner />
            {children}
          </div>
        </main>
      </div>
    </div>
    </ToastProvider>
  );
}
