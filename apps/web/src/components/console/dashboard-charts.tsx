"use client";

import {
  Area,
  AreaChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { DayPoint, StatusSlice } from "@/lib/dashboard-stats";

const axisStyle = {
  fontSize: 12,
  fill: "hsl(var(--muted-foreground))",
};

/** Compact, non-clipping axis labels: 1,200 / 12.5 / 0.06. */
function formatTick(v: number | string): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (n === 0) return "0";
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k`;
  if (Math.abs(n) >= 10) return n.toFixed(0);
  // Two significant digits keeps small values distinct (0.034, 0.026) without long tails.
  return String(Number(n.toPrecision(2)));
}

function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ name?: string; value?: number | string; color?: string }>;
  label?: string | number;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-border bg-surface px-3 py-2 text-xs shadow-md">
      {label != null ? (
        <p className="mb-1 font-medium text-foreground">{label}</p>
      ) : null}
      {payload.map((p, i) => (
        <p key={i} className="text-muted-foreground">
          <span style={{ color: p.color }}>{p.name}</span>: {p.value}
        </p>
      ))}
    </div>
  );
}

export function SpendAreaChart({ data }: { data: DayPoint[] }) {
  return (
    <div role="group" aria-label="Area chart of credits spent per day over the last 7 days">
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart
        data={data}
        margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
      >
        <defs>
          <linearGradient id="spendFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.4} />
            <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis dataKey="day" tickLine={false} axisLine={false} tick={axisStyle} />
        <YAxis tickLine={false} axisLine={false} tick={axisStyle} width={44} tickFormatter={formatTick} />
        <Tooltip content={<ChartTooltip />} />
        <Area
          type="monotone"
          dataKey="spend"
          name="Spend (credits)"
          stroke="hsl(var(--primary))"
          strokeWidth={2}
          fill="url(#spendFill)"
        />
      </AreaChart>
    </ResponsiveContainer>
    </div>
  );
}

export function ComputeHoursChart({ data }: { data: DayPoint[] }) {
  return (
    <div role="group" aria-label="Area chart of compute hours per day over the last 7 days">
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart
        data={data}
        margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
      >
        <defs>
          <linearGradient id="hoursFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--success))" stopOpacity={0.4} />
            <stop offset="100%" stopColor="hsl(var(--success))" stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis dataKey="day" tickLine={false} axisLine={false} tick={axisStyle} />
        <YAxis tickLine={false} axisLine={false} tick={axisStyle} width={44} tickFormatter={formatTick} />
        <Tooltip content={<ChartTooltip />} />
        <Area
          type="monotone"
          dataKey="hours"
          name="Compute hours"
          stroke="hsl(var(--success))"
          strokeWidth={2}
          fill="url(#hoursFill)"
        />
      </AreaChart>
    </ResponsiveContainer>
    </div>
  );
}

export function JobStatusChart({
  data,
  label = "Donut chart of your jobs by status; the list below gives the exact counts",
}: {
  data: StatusSlice[];
  label?: string;
}) {
  return (
    <div role="group" aria-label={label}>
    <ResponsiveContainer width="100%" height={220}>
      <PieChart>
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          innerRadius={55}
          outerRadius={85}
          paddingAngle={2}
          stroke="none"
        >
          {data.map((entry) => (
            <Cell key={entry.name} fill={entry.color} />
          ))}
        </Pie>
        <Tooltip content={<ChartTooltip />} />
      </PieChart>
    </ResponsiveContainer>
    </div>
  );
}

export interface UtilizationDatum {
  time: string;
  util: number;
}

/** GPU busy-ness over the last hour, 0–100 %. */
export function GpuUtilizationChart({ data }: { data: UtilizationDatum[] }) {
  return (
    <div role="group" aria-label="Area chart of how busy the graphics card was over the last hour">
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="gpuUtilFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.4} />
              <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="time" tickLine={false} axisLine={false} tick={axisStyle} minTickGap={32} />
          <YAxis
            tickLine={false}
            axisLine={false}
            tick={axisStyle}
            width={44}
            domain={[0, 100]}
            tickFormatter={(v) => `${v}%`}
          />
          <Tooltip content={<ChartTooltip />} />
          <Area
            type="monotone"
            dataKey="util"
            name="Busy (%)"
            stroke="hsl(var(--primary))"
            strokeWidth={2}
            fill="url(#gpuUtilFill)"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
