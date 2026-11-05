"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, CircleHelp, Coins, ListChecks, Receipt, RefreshCw, Timer, X } from "lucide-react";
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
import { jobTypeLabel, shortId } from "@/lib/jobs";
import { RelativeTime } from "@/components/console/bits";
import { JobStatusBadge } from "@/components/jobs/status-badge";
import { useAuth } from "@/lib/auth-context";

const GUIDE_DISMISS_KEY = "computecoin.firstJobGuide.dismissed";

const GUIDE_STEPS: { title: string; body: string; actions?: { href: string; label: string; primary?: boolean }[] }[] = [
  {
    title: "Add credits",
    body: "Credits pay for your jobs. In this preview they are test credits, not real money.",
    actions: [{ href: "/dashboard/billing", label: "Add credits", primary: true }],
  },
  {
    title: "Pick a Kit or write a prompt",
    body: "A Kit is a ready-made tool, like turning speech into text or making an image. Or write a prompt and we run it for you.",
    actions: [
      { href: "/dashboard/kits", label: "Browse Kits" },
      { href: "/dashboard/jobs/new", label: "Write a prompt" },
    ],
  },
  {
    title: "Submit it and watch it run",
    body: "Press submit. Your job waits for a host's computer, then runs. In this preview a job may run in practice mode. That means the result is a placeholder, not real AI output. The job page tells you which one you got.",
  },
  {
    title: "Read your results",
    body: "Open the job from your Jobs list. You will see its status, what it cost, a log of what happened, and files to download.",
  },
];

/** A short getting-started guide. Shown only to accounts with no jobs; the person can close it for good. */
function FirstJobGuide() {
  // null = not read yet. Nothing is shown until we know, so the card never flashes in and out.
  const [dismissed, setDismissed] = useState<boolean | null>(null);
  useEffect(() => {
    try {
      setDismissed(window.localStorage.getItem(GUIDE_DISMISS_KEY) === "1");
    } catch {
      setDismissed(false); // storage blocked: still show the guide
    }
  }, []);
  if (dismissed !== false) return null;

  function dismiss() {
    setDismissed(true);
    try {
      window.localStorage.setItem(GUIDE_DISMISS_KEY, "1");
    } catch {
      /* storage blocked: the guide is hidden until the next visit */
    }
  }

  return (
    <Card aria-labelledby="first-job-guide-title" role="region">
      <CardHeader className="flex-row items-start justify-between gap-3 space-y-0">
        <div>
          <h2 id="first-job-guide-title" className="text-lg font-semibold leading-none tracking-tight">
            Run your first job
          </h2>
          <p className="mt-1.5 text-sm text-muted-foreground">Four short steps. It takes a few minutes.</p>
        </div>
        <Button variant="ghost" size="sm" onClick={dismiss}>
          <X /> Dismiss guide
        </Button>
      </CardHeader>
      <CardContent>
        <ol className="space-y-5">
          {GUIDE_STEPS.map((step, i) => (
            <li key={step.title} className="flex gap-3">
              <span
                aria-hidden="true"
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground"
              >
                {i + 1}
              </span>
              <div className="min-w-0 space-y-2">
                <p className="font-medium">
                  <span className="sr-only">Step {i + 1}: </span>
                  {step.title}
                </p>
                <p className="text-sm text-muted-foreground">{step.body}</p>
                {step.actions ? (
                  <div className="flex flex-wrap gap-2">
                    {step.actions.map((a) => (
                      <Button key={a.href} asChild size="sm" variant={a.primary ? "default" : "outline"}>
                        <Link href={a.href}>{a.label}</Link>
                      </Button>
                    ))}
                  </div>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

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
    { label: "Your jobs", value: String(jobs.length), note: `${activeJobCount(jobs)} running or waiting`, icon: ListChecks },
    { label: "Credits spent", value: usage?.total_actual ?? "0.00", note: "paid for so far", icon: Receipt },
    { label: "Compute hours, last 7 days", value: hours7d.toFixed(2), note: `${spend7d.toFixed(2)} credits spent`, icon: Timer },
  ];

  const name = user?.display_name || email;

  return (
    <>
      <ConsolePageHeader
        title="Dashboard"
        description={
          name
            ? `Welcome back, ${name}. Here is what is happening with your jobs and credits.`
            : "Here is what is happening with your jobs and credits."
        }
        action={
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline">
              <Link href="/marketplace/gpus">
                See graphics cards (GPUs) <ArrowUpRight />
              </Link>
            </Button>
            <Button asChild>
              <Link href="/dashboard/jobs/new">Start a new job</Link>
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        <p className="-mt-2 text-sm text-muted-foreground">
          <Link href="/help" className="inline-flex items-center gap-1 text-primary underline-offset-2 hover:underline">
            <CircleHelp aria-hidden="true" className="h-4 w-4" /> Help and glossary
          </Link>{" "}
          explains the words used here in plain language.
        </p>
        {!loading && !failed && jobs.length === 0 ? <FirstJobGuide /> : null}
        {failed ? (
          <div
            role="alert"
            className="flex flex-col gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger sm:flex-row sm:items-center sm:justify-between"
          >
            <span>We could not load some of these numbers. Check your internet connection, then try again.</span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                void jobsQuery.refetch();
                void usageQuery.refetch();
                void accountQuery.refetch();
              }}
            >
              <RefreshCw /> Try again
            </Button>
          </div>
        ) : null}

        <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
          {tiles.map((t) => (
            <Card key={t.label}>
              <CardContent className="flex items-start justify-between gap-2 p-4 sm:p-5">
                <div className="min-w-0">
                  <p className="text-sm text-muted-foreground">{t.label}</p>
                  {loading ? (
                    <Skeleton className="mt-1 h-8 w-24" />
                  ) : (
                    <p className="mt-1 truncate font-mono text-2xl font-bold tracking-tight tabular-nums">{t.value}</p>
                  )}
                  <p className="mt-2 text-xs text-muted-foreground">{t.note}</p>
                </div>
                <span aria-hidden="true" className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary sm:flex">
                  <t.icon className="h-4 w-4" />
                </span>
              </CardContent>
            </Card>
          ))}
        </div>

        {!loading && !failed && jobs.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
              <h3 className="font-semibold">You have not run any jobs yet</h3>
              <p className="max-w-md text-sm text-muted-foreground">
                Your charts will show up here after your first job. Start a job and we will find
                a host&apos;s computer to run it. Or look through the available graphics cards first.
              </p>
              <div className="flex flex-wrap justify-center gap-2">
                <Button asChild>
                  <Link href="/dashboard/jobs/new">Start your first job</Link>
                </Button>
                <Button asChild variant="outline">
                  <Link href="/marketplace/gpus">See graphics cards (GPUs)</Link>
                </Button>
              </div>
            </CardContent>
          </Card>
        ) : (
          <>
            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Credits spent, last 7 days</CardTitle>
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
                  <CardTitle>Your jobs by status</CardTitle>
                </CardHeader>
                <CardContent>
                  <JobStatusChart data={slices} />
                  <ul className="mt-4 grid gap-2 text-sm sm:grid-cols-2 sm:gap-x-8">
                    {slices.map((s) => (
                      <li key={s.name} className="flex items-center gap-2">
                        <span aria-hidden="true" className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: s.color }} />
                        <span className="text-muted-foreground">{s.name}</span>
                        <span className="ml-auto font-medium">{s.value}</span>
                      </li>
                    ))}
                  </ul>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <CardTitle>Recent jobs</CardTitle>
                  <Button asChild variant="ghost" size="sm" className="-mr-3">
                    <Link href="/dashboard/jobs">View all</Link>
                  </Button>
                </CardHeader>
                <CardContent className="p-0">
                  <ul className="divide-y divide-border">
                    {jobs.slice(0, 5).map((job) => {
                      const id = job.job_id ?? job.id ?? "";
                      return (
                        <li key={id}>
                          <Link
                            href={`/dashboard/jobs/${id}`}
                            className="flex items-center gap-3 px-6 py-3 text-sm transition-colors hover:bg-surface-2/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                          >
                            <span className="font-mono text-xs text-primary" title={id}>
                              {shortId(id)}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-muted-foreground">
                              {jobTypeLabel(job.workload_type)}
                            </span>
                            <JobStatusBadge status={job.status} />
                            <RelativeTime iso={job.created_at} className="hidden text-xs text-muted-foreground sm:inline" />
                          </Link>
                        </li>
                      );
                    })}
                  </ul>
                </CardContent>
              </Card>
            </div>
          </>
        )}
      </div>
    </>
  );
}
