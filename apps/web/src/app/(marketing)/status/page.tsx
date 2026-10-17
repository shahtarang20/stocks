import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { DemoBanner } from "@/components/demo-banner";
import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = {
  title: "Status",
  description:
    "Service status placeholder. Phase 1 preview — not a live status page yet.",
};

const COMPONENTS = [
  { name: "Marketing site", state: "Operational", tone: "success" as const },
  { name: "Developer console", state: "Preview", tone: "warning" as const },
  { name: "Auth API", state: "Preview", tone: "warning" as const },
  { name: "Provider network", state: "Not yet online", tone: "neutral" as const },
  { name: "Job scheduler", state: "Not yet online", tone: "neutral" as const },
  { name: "Billing & metering", state: "Not yet online", tone: "neutral" as const },
];

export default function StatusPage() {
  return (
    <>
      <PageHero
        eyebrow="Status"
        title="System status"
        description="A real status page with uptime history and incident reporting ships before general availability. The board below is a labelled placeholder."
      />
      <div className="container space-y-8 py-12">
        <DemoBanner>
          this is a placeholder status board. The states shown are
          hand-authored to reflect Phase 1 scope and are not driven by live
          health checks or real uptime data.
        </DemoBanner>

        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <div className="flex items-center justify-between border-b border-border px-5 py-4">
            <span className="font-medium">Components</span>
            <span className="text-xs text-muted-foreground">
              Placeholder — not live
            </span>
          </div>
          <ul className="divide-y divide-border">
            {COMPONENTS.map((c) => (
              <li
                key={c.name}
                className="flex items-center justify-between px-5 py-4"
              >
                <span className="text-sm">{c.name}</span>
                <Badge variant={c.tone}>{c.state}</Badge>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}
