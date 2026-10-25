"use client";

import {
  LayoutDashboard,
  Rocket,
  Cpu,
  Wallet,
  Banknote,
  ArrowLeft,
} from "lucide-react";
import { AuthGuard } from "@/components/console/auth-guard";
import { ConsoleShell, type NavSection } from "@/components/console/console-shell";
import { ProviderGate } from "@/components/provider/provider-gate";

const SECTIONS: NavSection[] = [
  {
    title: "Provider",
    items: [
      { href: "/provider/dashboard", label: "Overview", icon: LayoutDashboard },
      { href: "/provider/onboarding", label: "Onboarding", icon: Rocket },
      { href: "/provider/hardware", label: "Hardware", icon: Cpu },
      { href: "/provider/earnings", label: "Earnings", icon: Wallet },
      { href: "/provider/payouts", label: "Payouts", icon: Banknote },
    ],
  },
  {
    title: "Account",
    items: [
      { href: "/dashboard", label: "Back to console", icon: ArrowLeft },
    ],
  },
];

export default function ProviderLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <AuthGuard>
      <ConsoleShell sections={SECTIONS}>
        <ProviderGate>{children}</ProviderGate>
      </ConsoleShell>
    </AuthGuard>
  );
}
