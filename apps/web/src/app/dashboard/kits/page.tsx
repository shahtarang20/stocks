"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, ArrowRight, Boxes, Search } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { kitsApi, type KitSummary } from "@/lib/kits";
import { humanize } from "@/lib/schema-form";
import { kitBlurb } from "@/lib/kit-blurbs";

export default function KitCataloguePage() {
  const [category, setCategory] = useState<string>("all");
  const [search, setSearch] = useState("");
  const query = useQuery<KitSummary[]>({ queryKey: ["kits"], queryFn: () => kitsApi.listKits() });

  const categories = useMemo(
    () => Array.from(new Set((query.data ?? []).map((k) => k.category))).sort(),
    [query.data],
  );
  const needle = search.trim().toLowerCase();
  const kits = (query.data ?? []).filter((k) => {
    if (category !== "all" && k.category !== category) return false;
    if (!needle) return true;
    const blurb = kitBlurb(k.slug);
    const haystack = [k.name, humanize(k.category), blurb?.does, blurb?.input, blurb?.output].join(" ").toLowerCase();
    return needle.split(/\s+/).every((word) => haystack.includes(word));
  });

  return (
    <>
      <ConsolePageHeader
        title="Kits"
        description="A Kit is a ready-made tool, like turning speech into text or making an image. Pick one, answer a few questions, upload your files and start. We take care of the rest."
      />

      <div className="mb-4 max-w-md space-y-1.5">
        <Label htmlFor="kit-search">Search Kits</Label>
        <div className="relative">
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            id="kit-search"
            type="search"
            className="pl-9"
            placeholder="For example: video, speech, pictures"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {categories.length > 1 ? (
        <div role="group" aria-label="Filter by category" className="mb-6 flex flex-wrap gap-2">
          {["all", ...categories].map((c) => (
            <Button
              key={c}
              type="button"
              size="sm"
              variant={category === c ? "default" : "outline"}
              aria-pressed={category === c}
              onClick={() => setCategory(c)}
            >
              {c === "all" ? "All" : humanize(c)}
            </Button>
          ))}
        </div>
      ) : null}

      {query.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-36 w-full" />
          ))}
        </div>
      ) : query.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex items-start gap-3 p-6" role="alert">
            <AlertCircle aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
            <div>
              <h2 className="font-semibold">We could not load the Kits</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {query.error instanceof ApiError ? query.error.message : "Something went wrong on our side. Try again in a moment."}
              </p>
              <Button className="mt-3" variant="outline" size="sm" onClick={() => void query.refetch()}>
                Try again
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : kits.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-2 p-10 text-center">
            <Boxes aria-hidden="true" className="h-6 w-6 text-muted-foreground" />
            {(query.data ?? []).length > 0 ? (
              <>
                <h2 className="font-semibold">No Kits match that</h2>
                <p className="max-w-md text-sm text-muted-foreground">Try fewer or different words, or clear the search to see every Kit.</p>
                <Button
                  className="mt-2"
                  variant="outline"
                  onClick={() => {
                    setSearch("");
                    setCategory("all");
                  }}
                >
                  Show all Kits
                </Button>
              </>
            ) : (
              <>
                <h2 className="font-semibold">No Kits are ready yet</h2>
                <p className="max-w-md text-sm text-muted-foreground">New Kits will show up here. In the meantime, you can write your own prompt.</p>
                <Button asChild className="mt-2"><Link href="/dashboard/jobs/new">Write a prompt</Link></Button>
              </>
            )}
          </CardContent>
        </Card>
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {kits.map((k) => (
            <li key={k.slug}>
              <Card className="h-full">
                <CardContent className="flex h-full flex-col gap-3 p-5">
                  <div className="flex items-start justify-between gap-2">
                    <h2 className="text-base font-semibold">{k.name}</h2>
                    <Badge variant="neutral">{humanize(k.category)}</Badge>
                  </div>
                  {(() => {
                    const blurb = kitBlurb(k.slug);
                    return blurb ? (
                      <div className="space-y-2 text-sm">
                        <p>{blurb.does}</p>
                        {blurb.input && blurb.output ? (
                          <dl className="space-y-0.5 text-xs text-muted-foreground">
                            <div>
                              <dt className="inline font-medium text-foreground">You give it: </dt>
                              <dd className="inline">{blurb.input}</dd>
                            </div>
                            <div>
                              <dt className="inline font-medium text-foreground">You get: </dt>
                              <dd className="inline">{blurb.output}</dd>
                            </div>
                          </dl>
                        ) : null}
                      </div>
                    ) : null;
                  })()}
                  <p className="text-xs text-muted-foreground">Version {k.version}</p>
                  {k.mode === "session" ? (
                    // An interactive Kit cannot be started as a batch job; Sessions opens it (and remembers the name).
                    <Button asChild className="mt-auto w-full sm:w-auto sm:self-start">
                      <Link href={`/dashboard/sessions?kit=${encodeURIComponent(`${k.slug}@${k.version}`)}`}>
                        Start a session <ArrowRight />
                      </Link>
                    </Button>
                  ) : k.mode && k.mode !== "batch" ? (
                    <p className="mt-auto text-sm text-muted-foreground">This Kit runs as an always-on service and is started through the API.</p>
                  ) : (
                    <Button asChild className="mt-auto w-full sm:w-auto sm:self-start">
                      <Link href={`/dashboard/kits/${encodeURIComponent(k.slug)}?v=${encodeURIComponent(k.version)}`}>
                        Start a job <ArrowRight />
                      </Link>
                    </Button>
                  )}
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
