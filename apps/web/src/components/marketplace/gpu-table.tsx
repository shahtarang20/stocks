"use client";

import { useMemo, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
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
import {
  DEMO_GPU_LISTINGS,
  DEMO_GPU_MODELS,
  DEMO_REGIONS,
  type VerificationStatus,
} from "@/lib/demo-data";
import { formatUsd } from "@/lib/utils";

const VERIFICATION_LABEL: Record<VerificationStatus, string> = {
  verified: "Verified",
  attested: "Attested",
  unverified: "Unverified",
};

function VerificationBadge({ status }: { status: VerificationStatus }) {
  const variant =
    status === "verified"
      ? "success"
      : status === "attested"
        ? "warning"
        : "neutral";
  return <Badge variant={variant}>{VERIFICATION_LABEL[status]}</Badge>;
}

const selectClass =
  "h-10 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

export function GpuTable({ limit }: { limit?: number }) {
  const [model, setModel] = useState("all");
  const [region, setRegion] = useState("all");
  const [verification, setVerification] = useState("all");
  const [minVram, setMinVram] = useState(0);
  const [maxPrice, setMaxPrice] = useState<number | "">("");

  const rows = useMemo(() => {
    const filtered = DEMO_GPU_LISTINGS.filter((row) => {
      if (model !== "all" && row.model !== model) return false;
      if (region !== "all" && row.region !== region) return false;
      if (verification !== "all" && row.verification !== verification) return false;
      if (row.vramGb < minVram) return false;
      if (maxPrice !== "" && row.pricePerHour > maxPrice) return false;
      return true;
    }).sort((a, b) => a.pricePerHour - b.pricePerHour);
    return limit ? filtered.slice(0, limit) : filtered;
  }, [model, region, verification, minVram, maxPrice, limit]);

  const showFilters = !limit;

  return (
    <div className="space-y-5">
      {showFilters ? (
        <div className="rounded-lg border border-border bg-surface p-4">
          <div className="mb-4 flex items-center gap-2 text-sm font-medium">
            <SlidersHorizontal className="h-4 w-4 text-muted-foreground" />
            Filters
          </div>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <div className="space-y-1.5">
              <Label htmlFor="f-model">GPU model</Label>
              <select
                id="f-model"
                className={selectClass}
                value={model}
                onChange={(e) => setModel(e.target.value)}
              >
                <option value="all">All models</option>
                {DEMO_GPU_MODELS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="f-region">Region</Label>
              <select
                id="f-region"
                className={selectClass}
                value={region}
                onChange={(e) => setRegion(e.target.value)}
              >
                <option value="all">All regions</option>
                {DEMO_REGIONS.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="f-verif">Verification</Label>
              <select
                id="f-verif"
                className={selectClass}
                value={verification}
                onChange={(e) => setVerification(e.target.value)}
              >
                <option value="all">Any status</option>
                <option value="verified">Verified</option>
                <option value="attested">Attested</option>
                <option value="unverified">Unverified</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="f-vram">Min VRAM: {minVram} GB</Label>
              <input
                id="f-vram"
                type="range"
                min={0}
                max={192}
                step={8}
                value={minVram}
                onChange={(e) => setMinVram(Number(e.target.value))}
                className="h-10 w-full cursor-pointer accent-primary"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="f-price">Max $/hr</Label>
              <Input
                id="f-price"
                type="number"
                min={0}
                step="0.01"
                placeholder="No limit"
                value={maxPrice}
                onChange={(e) =>
                  setMaxPrice(e.target.value === "" ? "" : Number(e.target.value))
                }
              />
            </div>
          </div>
          <div className="mt-4 flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              {rows.length} synthetic{" "}
              {rows.length === 1 ? "listing" : "listings"}
            </p>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setModel("all");
                setRegion("all");
                setVerification("all");
                setMinVram(0);
                setMaxPrice("");
              }}
            >
              Reset filters
            </Button>
          </div>
        </div>
      ) : null}

      <div className="overflow-hidden rounded-lg border border-border bg-surface">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>GPU model</TableHead>
              <TableHead>VRAM</TableHead>
              <TableHead>Region</TableHead>
              <TableHead>Interconnect</TableHead>
              <TableHead>Verification</TableHead>
              <TableHead className="text-right">Indicative $/hr</TableHead>
              <TableHead className="text-right">Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="py-12 text-center">
                  <p className="font-medium">No listings match these filters</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Try widening the VRAM or price range. (All rows are
                    synthetic demo data.)
                  </p>
                </TableCell>
              </TableRow>
            ) : (
              rows.map((row) => (
                <TableRow key={row.id}>
                  <TableCell className="font-medium">{row.model}</TableCell>
                  <TableCell>{row.vramGb} GB</TableCell>
                  <TableCell>
                    <span className="font-mono text-xs text-muted-foreground">
                      {row.region}
                    </span>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {row.interconnect}
                  </TableCell>
                  <TableCell>
                    <VerificationBadge status={row.verification} />
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatUsd(row.pricePerHour)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button variant="outline" size="sm" disabled>
                      Demo only
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground">
        Prices are synthetic, indicative placeholders for layout purposes and do
        not represent real availability or real market rates.
      </p>
    </div>
  );
}
