import type { Metadata } from "next";
import Link from "next/link";
import { Check } from "lucide-react";
import { PageHero } from "@/components/layout/page-hero";
import { DemoBanner } from "@/components/demo-banner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export const metadata: Metadata = {
  title: "Pricing",
  description:
    "How pricing works on ComputeCoin: market-based GPU rates with transparent per-second metering.",
};

const TIERS = [
  {
    name: "On-demand",
    tagline: "Pay per second for what you run.",
    points: [
      "Market rate at time of provisioning",
      "Per-second GPU metering",
      "No minimum commitment",
      "Choose any verification tier",
    ],
  },
  {
    name: "Reserved",
    tagline: "Hold capacity for predictable workloads.",
    points: [
      "Discounted rate for committed hours",
      "Priority scheduling",
      "Region and GPU pinning",
      "Verified providers only",
    ],
    featured: true,
  },
  {
    name: "Provider payouts",
    tagline: "For operators supplying hardware.",
    points: [
      "Keep the majority of job revenue",
      "Transparent payout schedule",
      "No exclusivity requirement",
      "Utilization and earnings reporting",
    ],
  },
];

export default function PricingPage() {
  return (
    <>
      <PageHero
        eyebrow="Pricing"
        title="Market-based pricing, metered per second"
        description="Rates are set by supply and demand across the network, not fixed by us. Every job carries an auditable usage record. We do not claim to always be the cheapest option — compare before you commit."
      />
      <div className="container space-y-10 py-12">
        <DemoBanner>
          the figures and structures below illustrate how pricing is organized.
          No live rates, token price, or trading metrics are shown, and exact
          numbers are not yet published.
        </DemoBanner>

        <div className="grid gap-6 lg:grid-cols-3">
          {TIERS.map((tier) => (
            <Card
              key={tier.name}
              className={
                tier.featured ? "border-primary/60 shadow-md" : undefined
              }
            >
              <CardHeader>
                <CardTitle className="text-lg">{tier.name}</CardTitle>
                <CardDescription>{tier.tagline}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <ul className="space-y-2.5">
                  {tier.points.map((p) => (
                    <li key={p} className="flex items-start gap-2 text-sm">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                      <span className="text-muted-foreground">{p}</span>
                    </li>
                  ))}
                </ul>
                <Button
                  asChild
                  className="w-full"
                  variant={tier.featured ? "default" : "outline"}
                >
                  <Link href="/register">Get started</Link>
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>

        <div className="rounded-lg border border-border bg-surface p-6">
          <h2 className="text-lg font-semibold">How metering works</h2>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            You are billed for the GPU-seconds a job actually consumes, plus any
            attached storage and egress. The usage record for each job is
            itemized and exportable, so an invoice can be reconciled against the
            underlying meter. Payment is escrowed at provisioning and released
            against verifiable completion signals.
          </p>
        </div>
      </div>
    </>
  );
}
