"use client";

import {
  Activity,
  ArrowLeft,
  BookOpen,
  Briefcase,
  Lock,
  MessageSquareWarning,
  Scale,
  Server,
  Settings,
  Users,
} from "lucide-react";
import { AuthGuard } from "@/components/console/auth-guard";
import { ConsoleShell, type NavSection } from "@/components/console/console-shell";

const SECTIONS: NavSection[] = [
  {
    title: "Overview",
    items: [
      { href: "/admin/overview", label: "Overview", icon: Activity },
    ],
  },
  {
    title: "Management",
    items: [
      { href: "/admin/users", label: "Users", icon: Users },
      { href: "/admin/providers", label: "Providers", icon: Server },
      { href: "/admin/jobs", label: "Jobs", icon: Briefcase },
      { href: "/admin/disputes", label: "Disputes", icon: MessageSquareWarning },
    ],
  },
  {
    title: "Finance",
    items: [
      { href: "/admin/billing", label: "Billing & reconciliation", icon: Scale },
    ],
  },
  {
    title: "Security & ops",
    items: [
      { href: "/admin/security", label: "Security posture", icon: Lock },
      { href: "/admin/audit-logs", label: "Audit logs", icon: BookOpen },
      { href: "/admin/configuration", label: "Configuration", icon: Settings },
    ],
  },
  {
    title: "Account",
    items: [{ href: "/dashboard", label: "Back to console", icon: ArrowLeft }],
  },
];

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <AuthGuard>
      <ConsoleShell sections={SECTIONS}>{children}</ConsoleShell>
    </AuthGuard>
  );
}
