import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { LiveGpuTable } from "@/components/marketplace/live-gpu-table";

export const metadata: Metadata = {
  title: "Computers for rent",
  description:
    "Search computers with a graphics card (GPU) that hosts have shared. Filter by GPU model, memory, price and region, then ask for a price quote.",
};

export default function GpuListingsPage() {
  return (
    <>
      <PageHero
        eyebrow="Marketplace"
        title="Computers for rent"
        description="These are computers that hosts have connected to ComputeCoin. Filter by graphics card (GPU) model, GPU memory, price and region. Then ask for a price quote. A quote holds the price for a short time."
      />
      <div className="container space-y-8 py-12">
        <LiveGpuTable />
      </div>
    </>
  );
}
