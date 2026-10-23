"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CalendarClock,
  CircleHelp,
  CreditCard,
  ExternalLink,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type VendorCard } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

/** Colour + label per status. "unknown" is deliberately NOT green: a service we
 *  cannot measure must never look healthy. */
const STATUS: Record<
  string,
  { label: string; badge: string; bar: string }
> = {
  critical: {
    label: "Over limit",
    badge: "border-red-500/40 bg-red-500/10 text-red-400",
    bar: "bg-red-500",
  },
  warning: {
    label: "Near limit",
    badge: "border-amber-500/40 bg-amber-500/10 text-amber-400",
    bar: "bg-amber-500",
  },
  watch: {
    label: "Watch",
    badge: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
    bar: "bg-yellow-400",
  },
  ok: {
    label: "Healthy",
    badge: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
    bar: "bg-emerald-500",
  },
  unknown: {
    label: "Not measurable",
    badge: "border-slate-500/40 bg-slate-500/10 text-slate-300",
    bar: "bg-slate-500",
  },
};

/** How a usage number was obtained. Shown on every card that has one. */
const SOURCE_LABEL: Record<string, string> = {
  measured: "counted by us — estimate",
  vendor_api: "read from vendor",
  manual: "entered by hand",
  unavailable: "no usage API",
};

function UsageBar({ pct, status }: { pct: number; status: string }) {
  const tone = STATUS[status] ?? STATUS.unknown;
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-white/10">
      <div
        className={`h-full rounded-full ${tone.bar}`}
        style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
      />
    </div>
  );
}

function ServiceCard({ v }: { v: VendorCard }) {
  const tone = STATUS[v.status] ?? STATUS.unknown;

  return (
    <Card className="flex h-full flex-col">
      <CardContent className="flex flex-1 flex-col gap-4 p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="truncate text-base font-semibold">{v.name}</h3>
              {v.dashboard_url ? (
                <a
                  href={v.dashboard_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={`Open the ${v.name} dashboard`}
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              ) : null}
            </div>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">
              {v.purpose}
            </p>
          </div>
          <Badge className={tone.badge}>{tone.label}</Badge>
        </div>

        {/* Usage */}
        <div className="space-y-1.5">
          {v.usage_pct !== null ? (
            <>
              <div className="flex items-baseline justify-between text-sm">
                <span className="font-medium">
                  {v.current_usage} / {v.quota_limit}{" "}
                  <span className="text-muted-foreground">{v.quota_unit}</span>
                </span>
                <span className="tabular-nums text-muted-foreground">
                  {v.usage_pct.toFixed(0)}%
                </span>
              </div>
              <UsageBar pct={v.usage_pct} status={v.status} />
            </>
          ) : (
            <div className="flex items-center gap-2 rounded-md border border-dashed border-white/15 px-3 py-2 text-xs text-muted-foreground">
              <CircleHelp className="h-3.5 w-3.5 shrink-0" />
              <span>
                {v.measurable
                  ? "No usage reading yet."
                  : v.quota_limit
                    ? "Usage cannot be read automatically."
                    : "Usage-billed — no fixed ceiling."}
              </span>
            </div>
          )}
          <p className="text-[11px] text-muted-foreground">
            Source: {SOURCE_LABEL[v.usage_source] ?? v.usage_source}
          </p>
        </div>

        {/* Plan & cost */}
        <div className="grid grid-cols-2 gap-3 border-t border-white/10 pt-3 text-xs">
          <div>
            <p className="text-muted-foreground">Plan</p>
            <p className="font-medium">{v.plan_name ?? "—"}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Cost</p>
            <p className="font-medium">
              {v.cost_amount ? (
                <>
                  {v.cost_currency} {v.cost_amount}
                  <span className="text-muted-foreground">
                    {" "}
                    /{" "}
                    {v.billing_cycle === "annual"
                      ? "yr"
                      : v.billing_cycle === "monthly"
                        ? "mo"
                        : v.billing_cycle}
                  </span>
                </>
              ) : (
                <span className="text-amber-400">not set</span>
              )}
            </p>
          </div>
        </div>

        {/* Renewal */}
        <div
          className={`flex items-start gap-2 rounded-md px-3 py-2 text-xs ${
            v.renewal_due
              ? "border border-amber-500/40 bg-amber-500/10 text-amber-300"
              : "bg-white/5 text-muted-foreground"
          }`}
        >
          <CalendarClock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {v.renewal_date ? (
            <span>
              Renews <strong>{v.renewal_date}</strong>
              {v.days_until_renewal !== null
                ? v.days_until_renewal < 0
                  ? ` — passed ${Math.abs(v.days_until_renewal)}d ago`
                  : v.days_until_renewal === 0
                    ? " — today"
                    : ` — in ${v.days_until_renewal}d`
                : null}
            </span>
          ) : (
            <span>
              Renewal date not set — add it to get a reminder before you are
              charged.
            </span>
          )}
        </div>

        <p className="mt-auto text-[11px] leading-relaxed text-muted-foreground">
          {v.detail}
        </p>
        {v.notes ? (
          <p className="text-[11px] leading-relaxed text-muted-foreground/80">
            {v.notes}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export default function AdminVendorsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const qc = useQueryClient();

  const vendors = useQuery({
    queryKey: ["admin", "vendors"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listVendors(token),
    refetchInterval: 60000,
  });

  const summary = useQuery({
    queryKey: ["admin", "vendors", "summary"],
    enabled: Boolean(accessToken),
    queryFn: () => api.vendorSummary(token),
    refetchInterval: 60000,
  });

  const alerts = useQuery({
    queryKey: ["admin", "vendors", "alerts"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listVendorAlerts(token, true),
    refetchInterval: 60000,
  });

  const evaluate = useMutation({
    mutationFn: () => api.evaluateVendors(token),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "vendors"] });
    },
  });

  const acknowledge = useMutation({
    mutationFn: (id: string) => api.acknowledgeVendorAlert(token, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "vendors", "alerts"] });
    },
  });

  const list = vendors.data ?? [];
  const s = summary.data;
  const open = alerts.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Third-party services"
        description="Every external service ComputeCoin depends on, its quota, its cost and its renewal date. Usage figures show where they came from — a number counted by us is an estimate, not the vendor's invoice."
        action={
          <Button
            variant="outline"
            onClick={() => evaluate.mutate()}
            disabled={evaluate.isPending}
          >
            <RefreshCw className={evaluate.isPending ? "animate-spin" : undefined} />
            Check now
          </Button>
        }
      />

      {s ? (
        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-5">
          {[
            { label: "Services", value: s.total },
            { label: "Over limit", value: s.critical, tone: "text-red-400" },
            { label: "Near limit", value: s.warning, tone: "text-amber-400" },
            { label: "Not measurable", value: s.unknown, tone: "text-slate-300" },
            { label: "Renewals due", value: s.renewals_due, tone: "text-amber-400" },
          ].map((k) => (
            <Card key={k.label}>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">{k.label}</p>
                <p className={`text-2xl font-semibold ${k.tone ?? ""}`}>
                  {k.value}
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : null}

      {s ? (
        <Card className="mb-6">
          <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 p-4 text-sm">
            <span className="flex items-center gap-2">
              <CreditCard className="h-4 w-4 text-muted-foreground" />
              Known monthly cost:{" "}
              <strong>
                {s.cost_currency} {s.known_monthly_cost}
              </strong>
            </span>
            {s.services_without_cost > 0 ? (
              <span className="text-amber-400">
                Partial — {s.services_without_cost} service
                {s.services_without_cost === 1 ? "" : "s"} have no cost set, so
                this is not your full bill.
              </span>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {open.length > 0 ? (
        <Card className="mb-6 border-amber-500/40 bg-amber-500/5">
          <CardContent className="space-y-2 p-4">
            <p className="flex items-center gap-2 text-sm font-semibold text-amber-300">
              <ShieldAlert className="h-4 w-4" />
              {open.length} open alert{open.length === 1 ? "" : "s"}
            </p>
            {open.map((a) => (
              <div
                key={a.id}
                className="flex items-start justify-between gap-3 rounded-md bg-black/20 px-3 py-2 text-xs"
              >
                <span>
                  <strong>{a.vendor_name ?? "Service"}</strong> — {a.message}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => acknowledge.mutate(a.id)}
                  disabled={acknowledge.isPending}
                >
                  Dismiss
                </Button>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      {vendors.isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} className="h-64 w-full" />
          ))}
        </div>
      ) : vendors.isError ? (
        <Card>
          <CardContent className="flex items-center gap-2 p-6 text-sm text-red-400">
            <AlertTriangle className="h-4 w-4" />
            Could not load services.
          </CardContent>
        </Card>
      ) : list.length === 0 ? (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            No services configured yet.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {list.map((v) => (
            <ServiceCard key={v.id} v={v} />
          ))}
        </div>
      )}
    </>
  );
}
