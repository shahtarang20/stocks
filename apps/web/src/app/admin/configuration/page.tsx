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
      { key: "Platform fee", value: "10%", note: "taken from every job" },
      { key: "Free credits at sign-up", value: "1,000.00 test credits", note: "test credits, not real money" },
      { key: "Currency", value: "Test credits", note: "not real money" },
      { key: "Shortest charge", value: "60 seconds", note: "a job under a minute is charged as one minute" },
    ],
  },
  {
    section: "Jobs and matching",
    items: [
      { key: "Longest a job runs by default", value: "300 seconds", note: "a customer can change it per job" },
      { key: "Job claim time", value: "60 seconds", note: "if the host goes quiet, the job goes back in line" },
      { key: "How a computer is picked for a job", value: "Price 50% / Reliability 30% / Availability 20%", note: "shown openly" },
      { key: "Most tries per job", value: "10", note: "can be set per job" },
    ],
  },
  {
    section: "Marketplace",
    items: [
      { key: "Price quote lasts", value: "120 seconds", note: "after that the customer asks for a new price" },
      { key: "Booked time is held for", value: "900 seconds", note: "the computer is kept free for 15 minutes" },
      { key: "Rows per page", value: "20", note: "up to 100" },
    ],
  },
  {
    section: "Sign-in and computers",
    items: [
      { key: "Sign-in lasts", value: "15 minutes", note: "then it renews by itself" },
      { key: "Stay signed in for", value: "14 days", note: "the renewal key changes each time" },
      { key: "Computer check-in every", value: "30 seconds", note: "a computer counts as online if seen in the last 90 seconds" },
    ],
  },
  {
    section: "AI answers (chat and embeddings)",
    items: [
      { key: "Request limit", value: "60 requests per 60 seconds", note: "for each user" },
      { key: "Longest answer by default", value: "4,096 tokens", note: "can be changed" },
      { key: "Embedding size", value: "256", note: "practice mode: made by a simple formula, not a real AI model" },
      { key: "Most items per embedding request", value: "100", note: "upper limit" },
    ],
  },
];

export default function AdminConfigurationPage() {
  return (
    <>
      <ConsolePageHeader
        title="Settings"
        description="The main rules ComputeCoin runs on, such as fees and time limits. You can only read them here. To change one, ask the engineering team."
      />

      <div className="space-y-6">
        <DemoBanner>
          These are the default settings used while building ComputeCoin. The live server may use different values, and secret values are never shown here.
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
          To change a setting, ask the engineering team. They change it on the server and restart the app.
        </p>
      </div>
    </>
  );
}
