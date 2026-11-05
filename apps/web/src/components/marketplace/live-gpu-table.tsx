"use client";

import { useMemo, useState } from "react";
import {
  AlertCircle,
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
  const [verification, setVerification] = useState("");
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
    if (verification) p.verification_status = verification;
    return p;
  }, [gpuModel, gpuVendor, gpuRuntime, minVram, maxPrice, region, workload, verification, page]);

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
    setVerification("");
    setPage(1);
  }

  function openQuote(m: MarketplaceMachine) {
    setQuoteTarget(m);
    setQuoteOpen(true);
  }

  return (
    <div className="space-y-5">
      {/* Filters */}
      <div className="rounded-lg border border-border bg-surface p-4">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-medium">
            <SlidersHorizontal className="h-4 w-4 text-muted-foreground" />
            Filters
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
        <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
          <div className="space-y-1.5">
            <Label htmlFor="f-model">Graphics card (GPU) model</Label>
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
            <Label htmlFor="f-vendor">GPU brand</Label>
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
            <Label htmlFor="f-runtime">GPU software (CUDA or ROCm)</Label>
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
              <option value="cuda">CUDA</option>
              <option value="rocm">ROCm</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="f-vram">Least GPU memory (MB)</Label>
            <Input
              id="f-vram"
              type="number"
              min={0}
              placeholder="For example: 24576"
              value={minVram}
              onChange={(e) => {
                setMinVram(e.target.value === "" ? "" : Number(e.target.value));
                setPage(1);
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="f-price">Highest price per hour</Label>
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
          <div className="space-y-1.5">
            <Label htmlFor="f-verif">Hardware check</Label>
            <select
              id="f-verif"
              className={selectClass}
              value={verification}
              onChange={(e) => {
                setVerification(e.target.value);
                setPage(1);
              }}
            >
              <option value="">Any</option>
              <option value="detected">Reported by the computer</option>
            </select>
          </div>
        </div>
        <div className="mt-4 flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
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
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Graphics card (GPU) and its memory</TableHead>
                <TableHead>Processor and memory</TableHead>
                <TableHead>Region</TableHead>
                <TableHead className="text-right">Price per hour</TableHead>
                <TableHead>Hardware check</TableHead>
                <TableHead className="text-right">Free spots</TableHead>
                <TableHead>Past jobs</TableHead>
                <TableHead>Last seen</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((m) => (
                <TableRow key={m.machine_id}>
                  <TableCell>
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="font-medium">
                        {m.gpu_model ?? "CPU-only"}
                        {m.gpu_count > 1 ? ` ×${m.gpu_count}` : ""}
                      </span>
                      {m.gpu_vendor ? (
                        <Badge
                          variant="neutral"
                          className={
                            m.gpu_vendor === "amd"
                              ? "border-red-600/30 text-red-700 bg-red-500/10 dark:border-red-500/30 dark:text-red-400 text-[10px] uppercase font-mono px-1.5 py-0"
                              : "border-emerald-600/30 text-emerald-700 bg-emerald-500/10 dark:border-emerald-500/30 dark:text-emerald-400 text-[10px] uppercase font-mono px-1.5 py-0"
                          }
                        >
                          {m.gpu_vendor}
                        </Badge>
                      ) : null}
                      {m.gpu_runtime ? (
                        <Badge
                          variant="neutral"
                          className="border-primary/30 text-primary bg-primary/10 text-[10px] uppercase font-mono px-1.5 py-0"
                        >
                          {m.gpu_runtime}
                        </Badge>
                      ) : null}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {m.gpu_model ? `${formatMbAsGb(m.vram_mb)} GPU memory` : "—"}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="text-sm">{m.cpu_model || "—"}</div>
                    <div className="text-xs text-muted-foreground">
                      {m.cpu_cores ? `${m.cpu_cores} CPU cores · ` : ""}
                      {formatMbAsGb(m.ram_mb)} memory
                    </div>
                  </TableCell>
                  <TableCell>
                    <span className="font-mono text-xs text-muted-foreground">
                      {m.region ?? "—"}
                    </span>
                  </TableCell>
                  <TableCell className="text-right">
                    {/* Decimal string, shown verbatim — never parseFloat. */}
                    <div className="font-mono tabular-nums">
                      {m.price_per_hour}
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      {CREDITS_LABEL}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="neutral" title="The computer reported this itself. ComputeCoin has not checked it.">
                      {m.verification_status === "detected"
                        ? "Reported by the computer"
                        : m.verification_status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums">
                    {m.available_slots} / {m.max_concurrent_jobs}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {reliabilityText(m)}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatRelativeTime(m.last_heartbeat_at)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => openQuote(m)}
                    >
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
        not checked it yet. Free spots shows how many jobs a computer can take
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
