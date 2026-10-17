import Link from "next/link";
import {
  ArrowRight,
  ShieldCheck,
  Globe2,
  Gauge,
  Lock,
  Boxes,
  Receipt,
  Workflow,
  BadgeCheck,
  Scale,
  Server,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

const DIFFERENTIATORS = [
  {
    icon: Globe2,
    title: "Open provider network",
    body: "Permissionless supply from independent operators and data centers, not a single cloud.",
  },
  {
    icon: BadgeCheck,
    title: "Hardware attestation",
    body: "Providers are tiered by verification level so you can choose the assurance you need.",
  },
  {
    icon: Receipt,
    title: "Transparent metering",
    body: "Per-second GPU accounting with an auditable usage record for every job.",
  },
  {
    icon: Scale,
    title: "Market-based pricing",
    body: "Rates set by supply and demand across regions — compared, not asserted.",
  },
  {
    icon: Workflow,
    title: "Standard tooling",
    body: "Bring containers and existing ML workflows; no proprietary SDK lock-in.",
  },
  {
    icon: Gauge,
    title: "Measured performance",
    body: "Published methodology for benchmarks instead of marketing throughput numbers.",
  },
  {
    icon: Lock,
    title: "Workload isolation",
    body: "Tenant isolation requirements and provider obligations defined up front.",
  },
  {
    icon: Boxes,
    title: "Region & GPU choice",
    body: "Filter by model, VRAM, interconnect, and region to match the job.",
  },
  {
    icon: ShieldCheck,
    title: "Settlement you can inspect",
    body: "Escrowed payment released against verifiable completion signals.",
  },
  {
    icon: Server,
    title: "Provider economics",
    body: "Operators keep utilization high with clear payout terms and no exclusivity.",
  },
];

export default function HomePage() {
  return (
    <>
      {/* Hero */}
      <section className="relative overflow-hidden border-b border-border">
        <div className="pointer-events-none absolute inset-0 hero-grid" aria-hidden />
        <div className="pointer-events-none absolute inset-0 hero-glow" aria-hidden />
        <div className="container relative py-24 sm:py-32">
          <div className="mx-auto max-w-3xl text-center">
            <Link
              href="/status"
              className="inline-flex items-center gap-2 rounded-full border border-border bg-surface/60 px-3 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              <span className="flex h-1.5 w-1.5 rounded-full bg-primary" />
              Phase 1 developer preview
            </Link>
            <h1 className="mt-6 text-balance text-4xl font-bold tracking-tight sm:text-6xl">
              Decentralized AI GPU compute marketplace
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-pretty text-lg text-muted-foreground">
              Rent and provide GPU capacity across an open network of operators.
              Choose hardware by model, VRAM, region, and verification level —
              with transparent metering and settlement you can inspect.
            </p>
            <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <Button asChild size="lg">
                <Link href="/register">
                  Get started <ArrowRight />
                </Link>
              </Button>
              <Button asChild size="lg" variant="outline">
                <Link href="/marketplace">Explore the marketplace</Link>
              </Button>
            </div>

            {/* Honesty note */}
            <div className="mx-auto mt-10 max-w-xl rounded-lg border border-border bg-surface/50 px-5 py-4 text-left text-sm text-muted-foreground">
              <p className="font-medium text-foreground">Measured, not promised.</p>
              <p className="mt-1">
                We do not claim decentralized compute is always cheaper, faster,
                or safer. Where we show numbers, we show how they were measured.
                This preview displays no token price, market cap, or trading
                volume — anywhere.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Differentiators */}
      <section className="container py-20">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
            What makes the network different
          </h2>
          <p className="mt-3 text-muted-foreground">
            Ten principles the marketplace is built around — framed as
            commitments to verify, not guarantees to take on faith.
          </p>
        </div>
        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {DIFFERENTIATORS.map((d) => (
            <Card key={d.title} className="animate-fade-in">
              <CardContent className="flex flex-col gap-3 p-6">
                <span className="flex h-10 w-10 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <d.icon className="h-5 w-5" />
                </span>
                <h3 className="font-semibold">{d.title}</h3>
                <p className="text-sm text-muted-foreground">{d.body}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="border-t border-border bg-surface/30">
        <div className="container flex flex-col items-center gap-6 py-20 text-center">
          <h2 className="max-w-xl text-2xl font-bold tracking-tight sm:text-3xl">
            Build on compute you can account for
          </h2>
          <p className="max-w-xl text-muted-foreground">
            Create an account to explore the developer console. Provisioning real
            workloads lands as the provider network comes online.
          </p>
          <div className="flex flex-col gap-3 sm:flex-row">
            <Button asChild size="lg">
              <Link href="/register">
                Create an account <ArrowRight />
              </Link>
            </Button>
            <Button asChild size="lg" variant="outline">
              <Link href="/developers">Read the developer overview</Link>
            </Button>
          </div>
        </div>
      </section>
    </>
  );
}
