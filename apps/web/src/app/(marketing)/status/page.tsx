import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { LiveStatus } from "@/components/status/live-status";

export const metadata: Metadata = {
  title: "Status",
  description: "Live health of the ComputeCoin API, database and cache.",
};

export default function StatusPage() {
  return (
    <>
      <PageHero
        eyebrow="Status"
        title="System status"
        description="A live check of the ComputeCoin API, refreshed every 30 seconds. Uptime history and incident reports are not available yet."
      />
      <div className="container space-y-8 py-12">
        <LiveStatus />
      </div>
    </>
  );
}
