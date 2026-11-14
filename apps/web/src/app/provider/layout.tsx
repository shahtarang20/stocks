"use client";

import {
  LayoutDashboard,
  Rocket,
  Cpu,
  Wallet,
  WalletCards,
  Banknote,
  Activity,
  ArrowLeft,
} from "lucide-react";
import { AuthGuard } from "@/components/console/auth-guard";
import { ConsoleShell, type NavSection } from "@/components/console/console-shell";
import { ProviderGate } from "@/components/provider/provider-gate";

const SECTIONS: NavSection[] = [
  {
    title: "Hosting",
    items: [
      { href: "/provider/dashboard", label: "Overview", icon: LayoutDashboard },
      { href: "/provider/onboarding", label: "Add my computer", icon: Rocket },
      { href: "/provider/hardware", label: "My computers", icon: Cpu },
      { href: "/provider/usage", label: "Usage", icon: Activity },
      { href: "/provider/earnings", label: "Earnings", icon: Wallet },
      { href: "/provider/payouts", label: "Payouts", icon: Banknote },
      { href: "/dashboard/wallet", label: "Crypto wallet", icon: WalletCards },
    ],
  },
  {
    title: "Account",
    items: [
      { href: "/dashboard", label: "Back to my console", icon: ArrowLeft },
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
