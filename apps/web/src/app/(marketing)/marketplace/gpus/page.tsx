import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import Link from "next/link";
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
        <div role="note" className="max-w-3xl rounded-lg border border-border bg-surface px-4 py-3 text-sm">
          <p className="font-semibold">Two ways to use these computers</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-muted-foreground">
            <li>
              <strong className="font-medium text-foreground">Kits:</strong> ready-made tools. Pick one, answer a few questions, upload your files and
              start. Nothing to set up.
            </li>
            <li>
              <strong className="font-medium text-foreground">Rent a GPU:</strong> run <em>your own</em> code, such as training a model, in JupyterLab or a
              terminal in your browser. Tick &ldquo;Only computers that allow container rental&rdquo; below to see the computers that offer it. The host can
              see your files, so use it for work that is not sensitive.{" "}
              <Link href="/dashboard/rent" className="text-primary underline underline-offset-2">Rent a GPU</Link> (sign in first).
            </li>
          </ul>
        </div>
        <LiveGpuTable />
      </div>
    </>
  );
}
