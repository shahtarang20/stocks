"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Settings } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { DemoBanner } from "@/components/demo-banner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const PLATFORM_CONFIG = [
  {
    section: "Billing",
    items: [
      { key: "Platform fee rate", value: "10%", note: "applied to every job" },
      { key: "Signup grant", value: "1,000.00 USDC_CREDITS", note: "simulated" },
      { key: "Currency", value: "USDC_CREDITS", note: "simulated, no real payment rail" },
      { key: "Min billable seconds", value: "60 s", note: "sub-minute jobs billed at 1 min" },
    ],
  },
  {
    section: "Jobs & scheduling",
    items: [
      { key: "Default max runtime", value: "300 s", note: "overridable per job" },
      { key: "Lease TTL", value: "60 s", note: "re-queued on expiry" },
      { key: "Scheduler weights", value: "Price 50% / Reliability 30% / Availability 20%", note: "transparent" },
      { key: "Max job retries", value: "10", note: "configurable per job" },
    ],
  },
  {
    section: "Marketplace",
    items: [
      { key: "Quote TTL", value: "120 s", note: "after expiry must re-request" },
      { key: "Reservation TTL", value: "900 s", note: "capacity held for 15 min" },
      { key: "Page size (default)", value: "20", note: "max 100" },
    ],
  },
  {
    section: "Auth & tokens",
    items: [
      { key: "Access token TTL", value: "15 min", note: "short-lived" },
      { key: "Refresh token TTL", value: "14 days", note: "rotating" },
      { key: "Heartbeat interval", value: "30 s", note: "machine online window: 90 s" },
    ],
  },
  {
    section: "Inference (Phase 6)",
    items: [
      { key: "Rate limit", value: "60 req / 60 s", note: "per user" },
      { key: "Max tokens (default)", value: "4,096", note: "configurable" },
      { key: "Embedding dim", value: "256", note: "simulated hash-based" },
      { key: "Max input items (embeddings)", value: "100", note: "batch cap" },
    ],
  },
];

export default function AdminConfigurationPage() {
  return (
    <>
      <ConsolePageHeader
        title="Platform configuration"
        description="Read-only view of active platform settings derived from environment variables. Changes require a deployment."
      />

      <div className="space-y-6">
        <DemoBanner>
          Configuration values shown are from the default development settings.
          Production values are set via environment variables and a secrets
          manager — they are never shown in the UI.
        </DemoBanner>

        {PLATFORM_CONFIG.map((section) => (
          <Card key={section.section}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Settings className="h-4 w-4 text-primary" />
                {section.section}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="divide-y divide-border">
                {section.items.map((item) => (
                  <div key={item.key} className="flex items-center justify-between gap-4 py-2.5">
                    <div>
                      <p className="text-sm font-medium">{item.key}</p>
                      {item.note && (
                        <p className="text-xs text-muted-foreground">{item.note}</p>
                      )}
                    </div>
                    <code className="rounded bg-surface-2 px-2 py-1 text-xs font-mono text-foreground">
                      {item.value}
                    </code>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        ))}

        <p className="text-xs text-muted-foreground">
          To change any setting, update the corresponding environment variable
          and redeploy. See <code>.env.example</code> for the full list.
        </p>
      </div>
    </>
  );
}
