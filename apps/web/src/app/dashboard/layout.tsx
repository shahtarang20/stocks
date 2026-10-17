"use client";

import {
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
      { href: "/dashboard/jobs", label: "Jobs", icon: ListChecks },
      {
        href: "/dashboard/reservations",
        label: "Reservations",
        icon: CalendarClock,
      },
      { href: "/dashboard/usage", label: "Usage", icon: BarChart3 },
    ],
  },
  {
    title: "Account",
    items: [
      { href: "/dashboard/billing", label: "Billing", icon: CreditCard },
      { href: "/dashboard/invoices", label: "Invoices", icon: FileText },
      { href: "/dashboard/disputes", label: "Disputes", icon: ShieldAlert },
      { href: "/dashboard/api-keys", label: "API keys", icon: KeyRound },
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
    ],
  },
  {
    title: "Provider",
    items: [
      { href: "/provider/dashboard", label: "Provider console", icon: Server },
    ],
  },
];

const ADMIN_SECTION: NavSection = {
  title: "Admin",
  items: [
    { href: "/admin/overview", label: "Admin console", icon: Server },
    { href: "/admin/billing", label: "Billing & reconciliation", icon: Scale },
  ],
};

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { isAdmin } = useAuth();
  const sections = isAdmin ? [...BASE_SECTIONS, ADMIN_SECTION] : BASE_SECTIONS;
  return (
    <AuthGuard>
      <ConsoleShell sections={sections}>{children}</ConsoleShell>
    </AuthGuard>
  );
}
