"use client";

import Link from "next/link";
import { ArrowUpRight, Cpu, DollarSign, ListChecks, Timer } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { DemoBanner } from "@/components/demo-banner";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  GpuHoursChart,
  JobStatusChart,
  SpendAreaChart,
} from "@/components/console/dashboard-charts";
import { DEMO_JOB_STATUS } from "@/lib/demo-data";
import { useAuth } from "@/lib/auth-context";

const STATS = [
  { label: "GPU hours (7d)", value: "353", icon: Timer, delta: "demo" },
  { label: "Spend (7d)", value: "$741", icon: DollarSign, delta: "demo" },
  { label: "Active jobs", value: "4", icon: ListChecks, delta: "demo" },
  { label: "Avg. utilization", value: "68%", icon: Cpu, delta: "demo" },
];

export default function DashboardPage() {
  const { email } = useAuth();

  return (
    <>
      <ConsolePageHeader
        title="Dashboard"
        description={
          email
            ? `Welcome back, ${email}. Here is an overview of your compute activity.`
            : "Overview of your compute activity."
        }
        action={
          <Button asChild variant="outline">
            <Link href="/marketplace/gpus">
              Browse GPUs <ArrowUpRight />
            </Link>
          </Button>
        }
      />

      <div className="space-y-6">
        <DemoBanner>
          every metric and chart on this dashboard is clearly-labelled
          placeholder data. It does not reflect real usage, spend, utilization,
          or any token metric.
        </DemoBanner>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {STATS.map((stat) => (
            <Card key={stat.label}>
              <CardContent className="flex items-start justify-between p-5">
                <div>
                  <p className="text-sm text-muted-foreground">{stat.label}</p>
                  <p className="mt-1 text-2xl font-bold tracking-tight">
                    {stat.value}
                  </p>
                  <Badge variant="neutral" className="mt-2">
                    {stat.delta}
                  </Badge>
                </div>
                <span className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <stat.icon className="h-4 w-4" />
                </span>
              </CardContent>
            </Card>
          ))}
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Spend (demo)</CardTitle>
            </CardHeader>
            <CardContent>
              <SpendAreaChart />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>GPU hours (demo)</CardTitle>
            </CardHeader>
            <CardContent>
              <GpuHoursChart />
            </CardContent>
          </Card>
        </div>

        <div className="grid gap-4 lg:grid-cols-[1fr_1.4fr]">
          <Card>
            <CardHeader>
              <CardTitle>Job status (demo)</CardTitle>
            </CardHeader>
            <CardContent>
              <JobStatusChart />
              <ul className="mt-4 grid grid-cols-2 gap-2 text-sm">
                {DEMO_JOB_STATUS.map((s) => (
                  <li key={s.name} className="flex items-center gap-2">
                    <span
                      className="h-2.5 w-2.5 rounded-full"
                      style={{ backgroundColor: s.color }}
                    />
                    <span className="text-muted-foreground">{s.name}</span>
                    <span className="ml-auto font-medium">{s.value}</span>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Getting started</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm text-muted-foreground">
              <p>
                Projects, API keys, jobs, billing and invoices are live against
                the ComputeCoin API. The charts above are demo data; every
                other page shows your real account.
              </p>
              <ul className="space-y-2">
                <li>
                  <Link
                    href="/dashboard/projects"
                    className="text-primary hover:underline"
                  >
                    View your projects
                  </Link>{" "}
                  (live).
                </li>
                <li>
                  <Link
                    href="/marketplace/gpus"
                    className="text-primary hover:underline"
                  >
                    Explore GPU listings
                  </Link>{" "}
                  (live machines from providers).
                </li>
                <li>
                  <Link
                    href="/dashboard/api-keys"
                    className="text-primary hover:underline"
                  >
                    Manage API keys
                  </Link>{" "}
                  (live).
                </li>
              </ul>
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
