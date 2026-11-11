"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import {
  Boxes,
  Terminal,
  LayoutDashboard,
  FolderGit2,
  ListChecks,
  CalendarClock,
  BarChart3,
  CreditCard,
  FileText,
  ShieldAlert,
  KeyRound,
  Settings,
  Server,
  Scale,
  Wallet,
  Sparkles,
} from "lucide-react";
import { AuthGuard } from "@/components/console/auth-guard";
import { ConsoleShell, type NavSection } from "@/components/console/console-shell";
import { useAuth } from "@/lib/auth-context";

const BASE_SECTIONS: NavSection[] = [
  {
    title: "Overview",
    items: [
      { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
      { href: "/dashboard/projects", label: "Projects", icon: FolderGit2 },
      { href: "/dashboard/kits", label: "Kits", icon: Boxes },
      { href: "/what-you-can-do", label: "What you can do", icon: Sparkles },
      { href: "/dashboard/sessions", label: "Sessions", icon: Terminal },
      { href: "/dashboard/jobs", label: "Jobs", icon: ListChecks },
      {
        href: "/dashboard/reservations",
        label: "Bookings",
        icon: CalendarClock,
      },
      { href: "/dashboard/usage", label: "Usage", icon: BarChart3 },
    ],
  },
  {
    title: "Account",
    items: [
      { href: "/dashboard/billing", label: "Credits and billing", icon: CreditCard },
      { href: "/dashboard/wallet", label: "Crypto wallet", icon: Wallet },
      { href: "/dashboard/invoices", label: "Invoices", icon: FileText },
      { href: "/dashboard/disputes", label: "Disputes", icon: ShieldAlert },
      { href: "/dashboard/api-keys", label: "API keys", icon: KeyRound },
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
    ],
  },
  {
    title: "Hosting",
    items: [
      { href: "/provider/dashboard", label: "Host console", icon: Server },
    ],
  },
];

const ADMIN_SECTION: NavSection = {
  title: "Admin",
  items: [
    { href: "/admin/overview", label: "Admin console", icon: Server },
    { href: "/admin/billing", label: "Billing and checks", icon: Scale },
  ],
};

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { isAdmin } = useAuth();
  const pathname = usePathname();
  // The session web view runs on a SEPARATE origin (NEXT_PUBLIC_SESSION_PROXY_ORIGIN) on purpose, so that
  // host-supplied pages never share an origin with the signed-in dashboard. That origin has no login session;
  // the one-time token and key arrive in the URL fragment. Wrapping it in AuthGuard bounced it to /login, so
  // the web view could never open. Render it bare there (checked after mount to keep hydration identical).
  const [bare, setBare] = useState(false);
  useEffect(() => {
    const proxy = (process.env.NEXT_PUBLIC_SESSION_PROXY_ORIGIN ?? "").replace(/\/$/, "");
    setBare(
      Boolean(proxy) &&
        window.location.origin === proxy &&
        /^\/dashboard\/sessions\/[^/]+\/web\/?$/.test(pathname ?? ""),
    );
  }, [pathname]);
  if (bare) return <main className="mx-auto max-w-6xl p-4">{children}</main>;
  const sections = isAdmin ? [...BASE_SECTIONS, ADMIN_SECTION] : BASE_SECTIONS;
  return (
    <AuthGuard>
      <ConsoleShell sections={sections}>{children}</ConsoleShell>
    </AuthGuard>
  );
}
