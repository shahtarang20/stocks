import type { Metadata } from "next";
import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { PageHero } from "@/components/layout/page-hero";
import { DemoBanner } from "@/components/demo-banner";
import { GpuTable } from "@/components/marketplace/gpu-table";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = {
  title: "Marketplace",
  description:
    "Browse GPU compute across the ComputeCoin network. Demo preview with synthetic data.",
};

export default function MarketplacePage() {
  return (
    <>
      <PageHero
        eyebrow="Marketplace"
        title="GPU compute across an open network"
        description="Browse capacity by model, VRAM, region, and verification level. The listing you see below is a synthetic preview — live provider inventory arrives as the network comes online."
      />
      <div className="container space-y-8 py-12">
        <DemoBanner />

        <div className="flex items-center justify-between gap-4">
          <h2 className="text-xl font-semibold">Featured demo listings</h2>
          <Button asChild variant="outline" size="sm">
            <Link href="/marketplace/gpus">
              Full listings with filters <ArrowRight />
            </Link>
          </Button>
        </div>

        <GpuTable limit={5} />
      </div>
    </>
  );
}
