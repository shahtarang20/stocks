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
import { DEMO_JOB_STATUS, DEMO_SPEND_SERIES } from "@/lib/demo-data";

const axisStyle = {
  fontSize: 12,
  fill: "hsl(var(--muted-foreground))",
};

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

export function SpendAreaChart() {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart
        data={DEMO_SPEND_SERIES}
        margin={{ top: 8, right: 8, left: -16, bottom: 0 }}
      >
        <defs>
          <linearGradient id="spendFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.4} />
            <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis dataKey="day" tickLine={false} axisLine={false} tick={axisStyle} />
        <YAxis tickLine={false} axisLine={false} tick={axisStyle} width={40} />
        <Tooltip content={<ChartTooltip />} />
        <Area
          type="monotone"
          dataKey="spend"
          name="Spend ($)"
          stroke="hsl(var(--primary))"
          strokeWidth={2}
          fill="url(#spendFill)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function GpuHoursChart() {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart
        data={DEMO_SPEND_SERIES}
        margin={{ top: 8, right: 8, left: -16, bottom: 0 }}
      >
        <defs>
          <linearGradient id="hoursFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--success))" stopOpacity={0.4} />
            <stop offset="100%" stopColor="hsl(var(--success))" stopOpacity={0} />
          </linearGradient>
        </defs>
        <XAxis dataKey="day" tickLine={false} axisLine={false} tick={axisStyle} />
        <YAxis tickLine={false} axisLine={false} tick={axisStyle} width={40} />
        <Tooltip content={<ChartTooltip />} />
        <Area
          type="monotone"
          dataKey="gpuHours"
          name="GPU hours"
          stroke="hsl(var(--success))"
          strokeWidth={2}
          fill="url(#hoursFill)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function JobStatusChart() {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <PieChart>
        <Pie
          data={DEMO_JOB_STATUS}
          dataKey="value"
          nameKey="name"
          innerRadius={55}
          outerRadius={85}
          paddingAngle={2}
          stroke="none"
        >
          {DEMO_JOB_STATUS.map((entry) => (
            <Cell key={entry.name} fill={entry.color} />
          ))}
        </Pie>
        <Tooltip content={<ChartTooltip />} />
      </PieChart>
    </ResponsiveContainer>
  );
}
