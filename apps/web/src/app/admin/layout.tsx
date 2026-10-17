"use client";

import { Scale, ArrowLeft } from "lucide-react";
import { AuthGuard } from "@/components/console/auth-guard";
import { ConsoleShell, type NavSection } from "@/components/console/console-shell";

const SECTIONS: NavSection[] = [
  {
    title: "Admin",
    items: [
      { href: "/admin/billing", label: "Billing & reconciliation", icon: Scale },
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
