"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Coins, ListChecks, Receipt, Timer } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import {
  ComputeHoursChart,
  JobStatusChart,
  SpendAreaChart,
} from "@/components/console/dashboard-charts";
import { activeJobCount, jobStatusBreakdown, lastDaysSeries } from "@/lib/dashboard-stats";
import { api, CREDITS_LABEL } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function DashboardPage() {
  const { email, user, accessToken } = useAuth();
  const token = accessToken as string;
  const enabled = Boolean(accessToken);

  const jobsQuery = useQuery({ queryKey: ["dash", "jobs"], enabled, queryFn: () => api.listJobs(token) });
  const usageQuery = useQuery({ queryKey: ["dash", "usage"], enabled, queryFn: () => api.getUsage(token) });
  const accountQuery = useQuery({ queryKey: ["dash", "account"], enabled, queryFn: () => api.getBillingAccount(token) });

  const jobs = jobsQuery.data ?? [];
  const usage = usageQuery.data;
  const records = usage?.usage_records ?? usage?.records ?? [];
  const loading = jobsQuery.isLoading || usageQuery.isLoading || accountQuery.isLoading;
  const failed = jobsQuery.isError || usageQuery.isError || accountQuery.isError;

  const series = lastDaysSeries(records, 7);
  const slices = jobStatusBreakdown(jobs);
  const hours7d = series.reduce((n, p) => n + p.hours, 0);
  const spend7d = series.reduce((n, p) => n + p.spend, 0);

  const tiles = [
    { label: "Available credits", value: accountQuery.data?.available ?? "—", note: CREDITS_LABEL, icon: Coins },
    { label: "Total jobs", value: String(jobs.length), note: `${activeJobCount(jobs)} in progress`, icon: ListChecks },
    { label: "Total spend", value: usage?.total_actual ?? "0.00", note: "settled, all time", icon: Receipt },
    { label: "Compute hours (7d)", value: hours7d.toFixed(2), note: `${spend7d.toFixed(2)} spent`, icon: Timer },
  ];

  const name = user?.display_name || email;

  return (
    <>
      <ConsolePageHeader
        title="Dashboard"
        description={
          name
            ? `Welcome back, ${name}. Here is an overview of your compute activity.`
            : "Overview of your compute activity."
        }
        action={
          <div className="flex gap-2">
            <Button asChild variant="outline">
              <Link href="/marketplace/gpus">
                Browse GPUs <ArrowUpRight />
              </Link>
            </Button>
            <Button asChild>
              <Link href="/dashboard/jobs/new">New job</Link>
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        {failed ? (
          <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            Some dashboard figures could not be loaded. Refresh the page to try again.
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {tiles.map((t) => (
            <Card key={t.label}>
              <CardContent className="flex items-start justify-between p-5">
                <div className="min-w-0">
                  <p className="text-sm text-muted-foreground">{t.label}</p>
                  {loading ? (
                    <Skeleton className="mt-2 h-8 w-20" />
                  ) : (
                    <p className="mt-1 truncate font-mono text-2xl font-bold tracking-tight tabular-nums">{t.value}</p>
                  )}
                  <p className="mt-2 text-xs text-muted-foreground">{t.note}</p>
                </div>
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <t.icon className="h-4 w-4" />
                </span>
              </CardContent>
            </Card>
          ))}
        </div>

        {!loading && !failed && jobs.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
              <h3 className="font-semibold">No jobs yet</h3>
              <p className="max-w-md text-sm text-muted-foreground">
                Your charts appear here once you run a job. Pick a machine from the live
                marketplace, or let the scheduler choose one for you.
              </p>
              <div className="flex gap-2">
                <Button asChild>
                  <Link href="/dashboard/jobs/new">Submit your first job</Link>
                </Button>
                <Button asChild variant="outline">
                  <Link href="/marketplace/gpus">Browse GPUs</Link>
                </Button>
              </div>
            </CardContent>
          </Card>
        ) : (
          <>
            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Spend, last 7 days</CardTitle>
                </CardHeader>
                <CardContent>
                  <SpendAreaChart data={series} />
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Compute hours, last 7 days</CardTitle>
                </CardHeader>
                <CardContent>
                  <ComputeHoursChart data={series} />
                </CardContent>
              </Card>
            </div>

            <div className="grid gap-4 lg:grid-cols-[1fr_1.4fr]">
              <Card>
                <CardHeader>
                  <CardTitle>Jobs by status</CardTitle>
                </CardHeader>
                <CardContent>
                  <JobStatusChart data={slices} />
                  <ul className="mt-4 grid grid-cols-2 gap-2 text-sm">
                    {slices.map((s) => (
                      <li key={s.name} className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: s.color }} />
                        <span className="text-muted-foreground">{s.name}</span>
                        <span className="ml-auto font-medium">{s.value}</span>
                      </li>
                    ))}
                  </ul>
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Quick links</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  {[
                    ["/dashboard/jobs", "Your jobs"],
                    ["/dashboard/billing", "Billing and credits"],
                    ["/dashboard/invoices", "Invoices"],
                    ["/dashboard/api-keys", "API keys"],
                    ["/marketplace/gpus", "Live GPU listings"],
                  ].map(([href, label]) => (
                    <p key={href}>
                      <Link href={href} className="text-primary hover:underline">
                        {label}
                      </Link>
                    </p>
                  ))}
                </CardContent>
              </Card>
            </div>
          </>
        )}
      </div>
    </>
  );
}
