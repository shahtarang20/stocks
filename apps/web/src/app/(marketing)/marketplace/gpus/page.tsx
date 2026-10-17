import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { LiveGpuTable } from "@/components/marketplace/live-gpu-table";

export const metadata: Metadata = {
  title: "GPU listings",
  description:
    "Search live, registered GPU machines by model, VRAM, price, region, workload, and verification status. Request a quote and reserve capacity.",
};

export default function GpuListingsPage() {
  return (
    <>
      <PageHero
        eyebrow="Marketplace"
        title="Live GPU listings"
        description="Search real machines registered on the network — online, available, and priced. Filter by model, VRAM, price, region, workload, and verification status, then request a quote."
      />
      <div className="container space-y-8 py-12">
        <LiveGpuTable />
      </div>
    </>
  );
}
