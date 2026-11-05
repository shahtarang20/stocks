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
    "How pricing works on ComputeCoin: hosts set the price, you see a quote first, and you pay by the second.",
};

const CARDS = [
  {
    name: "If you rent a computer",
    tagline: "You see the price before you pay.",
    points: [
      "Each host sets a price per hour for their computer",
      "You ask for a price quote first. The quote shows a service fee and the rate it uses",
      "A quote holds its price for a short time (2 minutes by default)",
      "You pay for the seconds your job really runs, not the full time you estimated",
    ],
  },
  {
    name: "How your credits are used",
    tagline: "Credits are set aside, then settled.",
    points: [
      "When a job starts, credits are held for it",
      "When it ends, you pay for the time used and the rest comes back",
      "If a job fails or you cancel, it is never billed as a finished job",
      "In this preview, credits are test credits, not real money",
    ],
    featured: true,
  },
  {
    name: "If you are a host",
    tagline: "You choose your price.",
    points: [
      "You set your own price per hour and when you are available",
      "You earn credits when a job finishes",
      "We do not promise any amount of work or earnings",
      "Paying out real money to hosts is not live yet",
    ],
  },
];

export default function PricingPage() {
  return (
    <>
      <PageHero
        eyebrow="Pricing"
        title="You pay by the second, and you see the price first"
        description="Hosts set the prices, so prices go up and down with supply and demand. We do not claim to be the cheapest. Compare before you decide."
      />
      <div className="container space-y-10 py-12">
        <DemoBanner>
          the numbers on this site are for the preview. Credits are test credits and no real
          money changes hands. Exact prices are not published yet. To see what hosts charge
          right now, open the list of computers for rent.
        </DemoBanner>

        <div className="grid gap-6 lg:grid-cols-3">
          {CARDS.map((tier) => (
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
                      <Check aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                      <span className="text-muted-foreground">{p}</span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          ))}
        </div>

        <div className="flex flex-wrap gap-3">
          <Button asChild>
            <Link href="/marketplace/gpus">See computers and prices</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/register">Create a free account</Link>
          </Button>
        </div>

        <div className="rounded-lg border border-border bg-surface p-6">
          <h2 className="text-lg font-semibold">How the bill is worked out</h2>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            You are billed for the seconds your job actually runs. Every job has an itemized
            usage record that you can check against your invoice. Credits are held when the job
            starts and settled when it ends. If something looks wrong, you can open a dispute
            from your dashboard.
          </p>
        </div>
      </div>
    </>
  );
}
