"use client";

import { useMemo, useState } from "react";
import {
  AlertCircle,
  ChevronDown,
  RefreshCw,
  Server,
  SlidersHorizontal,
} from "lucide-react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { QuoteDialog } from "@/components/marketplace/quote-dialog";
import {
  api,
  ApiError,
  CREDITS_LABEL,
  type MarketplaceMachine,
  type MarketplaceSearchParams,
} from "@/lib/api";
import { formatMbAsGb, formatRelativeTime } from "@/lib/utils";

const selectClass =
  "h-10 w-full rounded-md border border-input bg-background px-3 text-base text-foreground sm:text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

const PAGE_SIZE = 20;

/**
 * "At least N GB of graphics card memory", in the MB the API filters on. Each threshold sits a little BELOW the
 * marketed size, because cards report slightly less than their label (a "24 GB" card reports about 24 564 MB and an
 * "80 GB" card about 81 559 MB), so asking for exactly 24 576 MB would hide real 24 GB cards.
 */
export const MEMORY_CHOICES: { label: string; mb: number }[] = [
  { label: "8 GB or more", mb: 7680 },
  { label: "12 GB or more", mb: 11264 },
  { label: "16 GB or more", mb: 15360 },
  { label: "24 GB or more", mb: 23040 },
  { label: "48 GB or more", mb: 46080 },
  { label: "80 GB or more", mb: 76800 },
];

/** Honest reliability text — never fabricate a rating. */
function reliabilityText(m: MarketplaceMachine): string {
  const { completed_jobs, success_rate } = m.reliability;
  if (success_rate == null) return "No jobs yet";
  const pct = Math.round(success_rate * 100);
  return `${pct}% finished well · ${completed_jobs} job${completed_jobs === 1 ? "" : "s"}`;
}

export function LiveGpuTable() {
  // Applied filters drive the query; inputs are committed on submit.
  const [gpuModel, setGpuModel] = useState("");
  const [gpuVendor, setGpuVendor] = useState("");
  const [gpuRuntime, setGpuRuntime] = useState("");
  const [minVram, setMinVram] = useState<number | "">("");
  const [maxPrice, setMaxPrice] = useState("");
  const [region, setRegion] = useState("");
  const [workload, setWorkload] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [hostKind, setHostKind] = useState("");
  const [page, setPage] = useState(1);

  const [quoteTarget, setQuoteTarget] = useState<MarketplaceMachine | null>(
    null,
  );
  const [quoteOpen, setQuoteOpen] = useState(false);

  const params = useMemo<MarketplaceSearchParams>(() => {
    const p: MarketplaceSearchParams = { page, page_size: PAGE_SIZE };
    if (gpuModel.trim()) p.gpu_model = gpuModel.trim();
    if (gpuVendor) p.gpu_vendor = gpuVendor;
    if (gpuRuntime) p.gpu_runtime = gpuRuntime;
    if (minVram !== "") p.min_vram_mb = Number(minVram);
    if (maxPrice.trim()) p.max_price_per_hour = maxPrice.trim();
    if (region.trim()) p.region = region.trim();
    if (workload.trim()) p.workload = workload.trim();
    if (hostKind === "dedicated" || hostKind === "shared") p.host_kind = hostKind;
    return p;
  }, [gpuModel, gpuVendor, gpuRuntime, minVram, maxPrice, region, workload, hostKind, page]);

  const query = useQuery({
    queryKey: ["marketplace", "machines", params],
    queryFn: () => api.searchMachines(params),
    placeholderData: keepPreviousData,
  });

  const result = query.data;
  const items = result?.items ?? [];
  const total = result?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function resetFilters() {
    setGpuModel("");
    setGpuVendor("");
    setGpuRuntime("");
    setMinVram("");
    setMaxPrice("");
    setRegion("");
    setWorkload("");
    setHostKind("");
    setPage(1);
  }

  // Filters that live in the folded section: show how many are on, so a hidden filter never goes unnoticed.
  const moreFiltersOn = [gpuVendor, gpuRuntime, region.trim(), workload.trim()].filter(Boolean).length;

  function openQuote(m: MarketplaceMachine) {
    setQuoteTarget(m);
    setQuoteOpen(true);
  }

  return (
    <div className="space-y-5">
      {/* Filters */}
      <div className="rounded-lg border border-border bg-surface p-4">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <SlidersHorizontal className="h-4 w-4 text-muted-foreground" />
            Find a computer
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 sm:gap-4 lg:grid-cols-4">
          <div className="space-y-1.5">
            <Label htmlFor="f-model">Graphics card (GPU)</Label>
            <Input
              id="f-model"
              placeholder="For example: RTX 4090"
              value={gpuModel}
              onChange={(e) => {
                setGpuModel(e.target.value);
                setPage(1);
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="f-vram">Graphics card memory</Label>
            <select
              id="f-vram"
              className={selectClass}
              value={minVram === "" ? "" : String(minVram)}
              onChange={(e) => {
                setMinVram(e.target.value === "" ? "" : Number(e.target.value));
                setPage(1);
              }}
            >
              <option value="">Any amount</option>
              {MEMORY_CHOICES.map((c) => (
                <option key={c.mb} value={c.mb}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="f-price">Highest price per hour (credits)</Label>
            <Input
              id="f-price"
              inputMode="decimal"
              placeholder="For example: 2.50"
              value={maxPrice}
              onChange={(e) => {
                setMaxPrice(e.target.value);
                setPage(1);
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="f-kind">Kind of computer</Label>
            <select
              id="f-kind"
              className={selectClass}
              value={hostKind}
              onChange={(e) => {
                setHostKind(e.target.value);
                setPage(1);
              }}
            >
              <option value="">Any</option>
              <option value="dedicated">Dedicated (a server nobody else uses)</option>
              <option value="shared">Shared (a PC its owner also uses)</option>
            </select>
          </div>
        </div>

        <details
          className="group mt-4 rounded-md border border-border"
          open={moreOpen}
          onToggle={(e) => setMoreOpen((e.currentTarget as HTMLDetailsElement).open)}
        >
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-md px-3 py-2 text-sm font-medium hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <span>
              More filters
              {moreFiltersOn > 0 ? (
                <span className="ml-2 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                  {moreFiltersOn} on
                </span>
              ) : null}
            </span>
            <ChevronDown aria-hidden="true" className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180" />
          </summary>
          <div className="grid gap-3 border-t border-border p-3 sm:grid-cols-2 sm:gap-4 lg:grid-cols-4">
            <div className="space-y-1.5">
              <Label htmlFor="f-vendor">Graphics card brand</Label>
              <select
                id="f-vendor"
                className={selectClass}
                value={gpuVendor}
                onChange={(e) => {
                  setGpuVendor(e.target.value);
                  setPage(1);
                }}
              >
                <option value="">Any brand</option>
                <option value="nvidia">NVIDIA</option>
                <option value="amd">AMD</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="f-runtime">Graphics card software</Label>
              <select
                id="f-runtime"
                className={selectClass}
                value={gpuRuntime}
                onChange={(e) => {
                  setGpuRuntime(e.target.value);
                  setPage(1);
                }}
              >
                <option value="">Any</option>
                <option value="cuda">CUDA (used by NVIDIA)</option>
                <option value="rocm">ROCm (used by AMD)</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="f-region">Region</Label>
              <Input
                id="f-region"
                placeholder="For example: us-east"
                value={region}
                onChange={(e) => {
                  setRegion(e.target.value);
                  setPage(1);
                }}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="f-workload">Type of job</Label>
              <Input
                id="f-workload"
                placeholder="For example: text-generation"
                value={workload}
                onChange={(e) => {
                  setWorkload(e.target.value);
                  setPage(1);
                }}
              />
            </div>
          </div>
        </details>

        <div className="mt-4 flex items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {query.isLoading
              ? "Looking for computers…"
              : query.isError
                ? "The list is not available right now"
                : `${total} ${total === 1 ? "computer" : "computers"} online`}
          </p>
          <Button variant="ghost" size="sm" onClick={resetFilters}>
            Reset filters
          </Button>
        </div>
      </div>

      {/* Table / states */}
      {query.isLoading ? (
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4">
                <Skeleton className="h-10 w-10 rounded-md" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-1/3" />
                  <Skeleton className="h-3 w-1/2" />
                </div>
                <Skeleton className="h-8 w-20 rounded-md" />
              </div>
            ))}
          </CardContent>
        </Card>
      ) : query.isError ? (
        <Card className="border-danger/40" role="alert">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">We could not load the list of computers</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {query.error instanceof ApiError
                ? query.error.message
                : "Something went wrong. Check your internet connection, then press Try again."}
            </p>
            <Button variant="outline" onClick={() => query.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : items.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
              <Server className="h-6 w-6" />
            </span>
            <h3 className="font-semibold">No computers are online right now</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              This list only shows computers that are online, free to use and
              priced. Nothing matches right now. Try again later, or press
              Reset filters to see everything.
            </p>
            <Button variant="outline" onClick={resetFilters}>
              Reset filters
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border bg-surface">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Graphics card (GPU)</TableHead>
                <TableHead>Computer</TableHead>
                <TableHead className="text-right">Price per hour</TableHead>
                <TableHead>Free now</TableHead>
                <TableHead>Track record</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Action</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((m) => (
                <TableRow key={m.machine_id}>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-medium">
                        {m.gpu_model ?? "CPU-only"}
                        {m.gpu_count > 1 ? ` ×${m.gpu_count}` : ""}
                      </span>
                      {m.gpu_vendor ? (
                        <Badge
                          variant="neutral"
                          title={m.gpu_runtime ? `Uses ${m.gpu_runtime.toUpperCase()} software` : undefined}
                          className={
                            m.gpu_vendor === "amd"
                              ? "border-red-600/30 text-red-700 bg-red-500/10 dark:border-red-500/30 dark:text-red-400 text-[10px] uppercase font-mono px-1.5 py-0"
                              : "border-emerald-600/30 text-emerald-700 bg-emerald-500/10 dark:border-emerald-500/30 dark:text-emerald-400 text-[10px] uppercase font-mono px-1.5 py-0"
                          }
                        >
                          {m.gpu_vendor}
                        </Badge>
                      ) : null}
                      {m.host_kind === "dedicated" || m.host_kind === "shared" ? (
                        <Badge
                          variant="neutral"
                          title={
                            m.host_kind === "dedicated"
                              ? "A server that only runs ComputeCoin jobs. Declared by the host."
                              : "A PC its owner also uses. Your job may run slower or be moved to another computer. Declared by the host."
                          }
                          className="text-[10px] px-1.5 py-0"
                        >
                          {m.host_kind === "dedicated" ? "Dedicated" : "Shared PC"}
                        </Badge>
                      ) : null}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {m.gpu_model ? `${formatMbAsGb(m.vram_mb)} memory` : "—"}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="text-sm">{m.cpu_model || "—"}</div>
                    <div className="text-xs text-muted-foreground">
                      {m.cpu_cores ? `${m.cpu_cores} CPU cores · ` : ""}
                      {formatMbAsGb(m.ram_mb)} memory
                      {m.region ? ` · ${m.region}` : ""}
                    </div>
                  </TableCell>
                  <TableCell className="text-right">
                    {/* Decimal string, shown verbatim — never parseFloat. */}
                    <div className="font-mono tabular-nums">{m.price_per_hour}</div>
                    <div className="text-[11px] text-muted-foreground">{CREDITS_LABEL}</div>
                  </TableCell>
                  <TableCell>
                    <div className="text-sm tabular-nums">
                      {m.available_slots} of {m.max_concurrent_jobs}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Seen {formatRelativeTime(m.last_heartbeat_at)}
                    </div>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{reliabilityText(m)}</TableCell>
                  <TableCell className="text-right">
                    <Button variant="outline" size="sm" onClick={() => openQuote(m)}>
                      Get a price quote
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Pagination */}
      {total > PAGE_SIZE ? (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            Page {page} of {totalPages}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1 || query.isFetching}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages || query.isFetching}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      ) : null}

      <p className="text-xs text-muted-foreground">
        These computers are shared by hosts. Prices are in {CREDITS_LABEL}, not
        real money. Each computer reports its own hardware, and ComputeCoin has
        not checked it yet. &quot;Free now&quot; shows how many jobs a computer can take
        at the same time.
      </p>

      <QuoteDialog
        machine={quoteTarget}
        open={quoteOpen}
        onOpenChange={setQuoteOpen}
      />
    </div>
  );
}
