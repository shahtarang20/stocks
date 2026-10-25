"use client";

import {
  Activity,
  ArrowLeft,
  BookOpen,
  Briefcase,
  Coins,
  Cpu,
  Lock,
  MessageSquareWarning,
  Scale,
  Server,
  Settings,
  Wallet,
  Users,
} from "lucide-react";
import { AuthGuard } from "@/components/console/auth-guard";
import { ConsoleShell, type NavSection } from "@/components/console/console-shell";

const SECTIONS: NavSection[] = [
  {
    title: "Overview",
    items: [
      { href: "/admin/overview", label: "Overview", icon: Activity },
      { href: "/admin/telemetry", label: "Telemetry & health", icon: Cpu },
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
      { href: "/admin/settlement", label: "On-chain settlement", icon: Coins },
      { href: "/admin/token-deposits", label: "Token deposits", icon: Coins },
    ],
  },
  {
    title: "Security & ops",
    items: [
      { href: "/admin/security", label: "Security posture", icon: Lock },
      { href: "/admin/audit-logs", label: "Audit logs", icon: BookOpen },
      { href: "/admin/configuration", label: "Configuration", icon: Settings },
      { href: "/admin/vendors", label: "Third-party services", icon: Wallet },
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
