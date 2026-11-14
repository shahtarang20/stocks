"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Cpu, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { EmptyState, ErrorState, RelativeTime } from "@/components/console/bits";
import { GpuUtilizationChart, JobStatusChart } from "@/components/console/dashboard-charts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError, type GpuReading, type Machine, type MachineLive, type MachineUsage } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import {
  formatDuration,
  liveStatusLine,
  memoryPercent,
  outcomeRows,
  tempTone,
  tempWords,
  usageSlices,
} from "@/lib/host-usage";
import { parseApiDate } from "@/lib/format";

const PERIODS = [1, 7, 14] as const;
const LIVE_REFRESH_MS = 10_000;
const USAGE_REFRESH_MS = 60_000;

function Meter({ label, percent, detail }: { label: string; percent: number | null; detail: string }) {
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono tabular-nums">{detail}</span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent ?? undefined}
        aria-valuetext={percent == null ? "No reading" : `${percent}%`}
        className="mt-1 h-2 overflow-hidden rounded-full bg-surface-2"
      >
        <div className="h-full rounded-full bg-primary" style={{ width: `${percent ?? 0}%` }} />
      </div>
    </div>
  );
}

function GpuCard({ gpu, many }: { gpu: GpuReading; many: boolean }) {
  const util = gpu.gpu_util_pct == null ? null : Math.round(gpu.gpu_util_pct);
  const mem = memoryPercent(gpu.mem_used_mb, gpu.mem_total_mb);
  const tone = tempTone(gpu.temp_c);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-base">
          <span>{many ? `Graphics card ${gpu.gpu_index + 1}` : "Graphics card"}</span>
          <Badge variant={tone === "hot" ? "danger" : tone === "warm" ? "warning" : "neutral"}>
            {gpu.temp_c == null ? tempWords(tone) : `${Math.round(gpu.temp_c)} °C · ${tempWords(tone)}`}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <Meter label="How busy" percent={util} detail={util == null ? "no reading" : `${util}%`} />
        <Meter
          label="Memory in use"
          percent={mem}
          detail={
            gpu.mem_used_mb == null || gpu.mem_total_mb == null
              ? "no reading"
              : `${Math.round(gpu.mem_used_mb / 1024 * 10) / 10} of ${Math.round(gpu.mem_total_mb / 1024 * 10) / 10} GB`
          }
        />
        {gpu.power_w != null ? (
          <p className="text-sm text-muted-foreground">
            Power draw: <span className="font-mono tabular-nums">{Math.round(gpu.power_w)} W</span>
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function LiveSection({ live }: { live: MachineLive }) {
  const chart = useMemo(
    () =>
      live.utilization_last_hour
        .filter((p) => p.avg_gpu_util_pct != null)
        .map((p) => ({
          time: parseApiDate(p.timestamp).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }),
          util: Math.round(p.avg_gpu_util_pct as number),
        })),
    [live.utilization_last_hour],
  );
  return (
    <section aria-labelledby="live-heading" className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="live-heading" className="text-lg font-semibold">Right now</h2>
        <Badge variant={live.running_jobs > 0 ? "default" : "neutral"}>
          {live.running_jobs > 0
            ? `${live.running_jobs} customer job${live.running_jobs === 1 ? "" : "s"} running`
            : "No customer job running"}
        </Badge>
      </div>
      <p className="text-sm text-muted-foreground" role="status">
        {liveStatusLine(live)}
        {live.last_reading_at ? (
          <>
            {" "}Last reading <RelativeTime iso={live.last_reading_at} />.
          </>
        ) : null}
      </p>
      {live.readings_recent && live.gpus.length > 0 ? (
        <div className="grid gap-4 md:grid-cols-2">
          {live.gpus.map((g) => (
            <GpuCard key={g.gpu_index} gpu={g} many={live.gpus.length > 1} />
          ))}
        </div>
      ) : (
        <EmptyState icon={Cpu} title="No live numbers to show">
          The numbers appear here once the Computecoin app on your computer is running and sending readings. Nothing on this
          page is guessed. If the app is running and this stays empty, check that your computer is turned on and online.
        </EmptyState>
      )}
      {chart.length > 1 ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">How busy the graphics card was in the last hour</CardTitle>
          </CardHeader>
          <CardContent>
            <GpuUtilizationChart data={chart} />
          </CardContent>
        </Card>
      ) : null}
    </section>
  );
}

function UsageSection({
  usage,
  days,
  onDays,
  loading,
}: {
  usage: MachineUsage | undefined;
  days: number;
  onDays: (d: number) => void;
  loading: boolean;
}) {
  const slices = usageSlices(usage);
  const outcomes = outcomeRows(usage?.attempts);
  return (
    <section aria-labelledby="usage-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="usage-heading" className="text-lg font-semibold">How your computer&apos;s time was used</h2>
        <div role="group" aria-label="Period" className="flex gap-2">
          {PERIODS.map((d) => (
            <Button
              key={d}
              size="sm"
              variant={d === days ? "default" : "outline"}
              aria-pressed={d === days}
              onClick={() => onDays(d)}
            >
              {d === 1 ? "Last 24 hours" : `Last ${d} days`}
            </Button>
          ))}
        </div>
      </div>
      {loading || !usage ? (
        <Skeleton className="h-56 w-full" />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          <Card>
            <CardContent className="p-5">
              {slices.length > 0 ? (
                <>
                  <JobStatusChart
                    data={slices}
                    label="Donut chart of how your computer's time was used; the list below gives the exact times"
                  />
                  <ul className="mt-2 space-y-1 text-sm">
                    {slices.map((s) => (
                      <li key={s.name} className="flex items-center justify-between gap-3">
                        <span className="flex items-center gap-2">
                          <span aria-hidden="true" className="inline-block h-3 w-3 rounded-sm" style={{ background: s.color }} />
                          {s.name}
                        </span>
                        <span className="font-mono tabular-nums">
                          {formatDuration(s.seconds)} · {s.percent}%
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="py-10 text-center text-sm text-muted-foreground">
                  Nothing to show for this period yet.
                </p>
              )}
              {!usage.telemetry_available ? (
                <p className="mt-3 rounded-md bg-surface-2 p-3 text-sm text-muted-foreground">
                  Your computer sent no readings in this period, so the rest of the time cannot be split into
                  &ldquo;ready and waiting&rdquo; and &ldquo;not reporting&rdquo;. Only the time spent on customer jobs is shown.
                </p>
              ) : null}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-base">What happened to the jobs sent to your computer</CardTitle>
            </CardHeader>
            <CardContent>
              {outcomes.length === 0 ? (
                <p className="text-sm text-muted-foreground">No customer jobs ran on this computer in this period.</p>
              ) : (
                <ul className="divide-y divide-border">
                  {outcomes.map((o) => (
                    <li key={o.key} className="flex items-start justify-between gap-3 py-2">
                      <div>
                        <p className="text-sm font-medium">{o.label}</p>
                        {o.hint ? <p className="text-xs text-muted-foreground">{o.hint}</p> : null}
                      </div>
                      <span className="font-mono text-lg tabular-nums">{o.count}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        These are estimates built from what your computer reported. The page never shows customers&apos; names, files or
        what a job was doing. Times are rounded to the nearest minute.
      </p>
    </section>
  );
}

export default function ProviderUsagePage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const [machineId, setMachineId] = useState<string>("");
  const [days, setDays] = useState<number>(7);

  const machines = useQuery<Machine[]>({
    queryKey: ["provider-machines"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listMachines(token),
  });
  const list = useMemo(() => machines.data ?? [], [machines.data]);
  useEffect(() => {
    if (!machineId && list.length > 0) setMachineId(list[0].machine_id);
  }, [list, machineId]);

  const live = useQuery<MachineLive>({
    queryKey: ["machine-live", machineId],
    enabled: Boolean(accessToken && machineId),
    queryFn: () => api.getMachineLive(token, machineId),
    refetchInterval: LIVE_REFRESH_MS,
  });
  const usage = useQuery<MachineUsage>({
    queryKey: ["machine-usage", machineId, days],
    enabled: Boolean(accessToken && machineId),
    queryFn: () => api.getMachineUsage(token, machineId, days),
    refetchInterval: USAGE_REFRESH_MS,
  });

  const refreshing = live.isFetching || usage.isFetching;

  return (
    <>
      <ConsolePageHeader
        title="Usage"
        description="See what your computer is doing right now and how its time was used. This is only about your own computers."
        action={
          <Button
            variant="outline"
            onClick={() => {
              live.refetch();
              usage.refetch();
            }}
            disabled={refreshing || !machineId}
          >
            <RefreshCw className={refreshing ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {machines.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : machines.isError ? (
        <ErrorState
          title="We could not load your computers"
          message={machines.error instanceof ApiError ? machines.error.message : undefined}
          onRetry={() => machines.refetch()}
          retrying={machines.isFetching}
        />
      ) : list.length === 0 ? (
        <EmptyState
          icon={Activity}
          title="No computers yet"
          action={
            <Button asChild>
              <Link href="/provider/onboarding">Add my computer</Link>
            </Button>
          }
        >
          Once you add a computer and it starts working, you will see how busy it is here.
        </EmptyState>
      ) : (
        <div className="space-y-8">
          {list.length > 1 ? (
            <div className="flex items-center gap-3">
              <label htmlFor="usage-machine" className="text-sm font-medium">Computer</label>
              <select
                id="usage-machine"
                value={machineId}
                onChange={(e) => setMachineId(e.target.value)}
                className="h-10 rounded-md border border-border bg-surface px-3 text-sm"
              >
                {list.map((m) => (
                  <option key={m.machine_id} value={m.machine_id}>
                    {m.hostname}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          {live.isError ? (
            <ErrorState
              title="We could not load the live numbers"
              message={live.error instanceof ApiError ? live.error.message : undefined}
              onRetry={() => live.refetch()}
              retrying={live.isFetching}
            />
          ) : live.data ? (
            <LiveSection live={live.data} />
          ) : (
            <Skeleton className="h-40 w-full" />
          )}

          {usage.isError ? (
            <ErrorState
              title="We could not load the usage history"
              message={usage.error instanceof ApiError ? usage.error.message : undefined}
              onRetry={() => usage.refetch()}
              retrying={usage.isFetching}
            />
          ) : (
            <UsageSection usage={usage.data} days={days} onDays={setDays} loading={usage.isLoading} />
          )}
        </div>
      )}
    </>
  );
}
