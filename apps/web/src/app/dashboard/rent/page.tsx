"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Server } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { api, apiErrorCode, ApiError, CREDITS_LABEL, type MarketplaceMachine } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { dataApi, isDataPlaneDisabled, type DataFile } from "@/lib/kits";
import { sessionsApi, type RentalTemplate } from "@/lib/sessions";
import { newClientKey, storeClientKey } from "@/lib/session-crypto";
import { formatBytes, formatMbAsGb } from "@/lib/utils";
import {
  EGRESS_CHOICES,
  EMPTY_FORM,
  RENTAL_ACCEPT_LABEL,
  RENTAL_LIMITS,
  RENTAL_WARNING_POINTS,
  buildRentalRequest,
  holdEstimate,
  inputProblems,
  priceBelow,
  rentalErrorText,
  suggestInputPath,
  webViewAvailable,
  type FormErrors,
  type RentalForm,
} from "@/lib/rental";

// A program with its own web page (JupyterLab) needs the separate session web address; without it the browser refuses to open one.
const WEB_VIEW = webViewAvailable(process.env.NEXT_PUBLIC_SESSION_PROXY_ORIGIN, process.env.NEXT_PUBLIC_SESSION_PROXY_ALLOW_SAME_ORIGIN);

const selectClass =
  "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-base text-foreground shadow-sm sm:text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

function machineLabel(m: MarketplaceMachine): string {
  const card = `${m.gpu_model ?? "CPU-only"}${m.gpu_count > 1 ? ` ×${m.gpu_count}` : ""}`;
  const memory = m.gpu_model ? ` · ${formatMbAsGb(m.vram_mb)} memory` : "";
  const region = m.region ? ` · ${m.region}` : "";
  return `${card}${memory} · ${m.price_per_hour} ${CREDITS_LABEL} per hour${region}`;
}

function FieldError({ id, text }: { id: string; text?: string }) {
  return text ? (
    <p id={id} role="alert" className="text-xs text-danger">
      {text}
    </p>
  ) : null;
}

export default function RentPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const [form, setForm] = useState<RentalForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<FormErrors>({});
  const [inputRows, setInputRows] = useState<Record<number, string>>({});
  const [wantedMachine, setWantedMachine] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const set = <K extends keyof RentalForm>(key: K, value: RentalForm[K]) => setForm((f) => ({ ...f, [key]: value }));

  // The marketplace links here as /dashboard/rent?machine=<id>.
  useEffect(() => {
    const m = new URLSearchParams(window.location.search).get("machine");
    if (m) setWantedMachine(m.slice(0, 64));
  }, []);

  const machines = useQuery({
    queryKey: ["rent", "machines"],
    queryFn: () => api.searchMachines({ container_rental: true, page_size: 100 }),
  });
  const templatesQuery = useQuery<RentalTemplate[]>({
    queryKey: ["rent", "templates"],
    queryFn: () => sessionsApi.listTemplates(),
    retry: false,
  });
  const filesQuery = useQuery<DataFile[]>({
    queryKey: ["rent", "files"],
    enabled: Boolean(accessToken),
    queryFn: () => dataApi.listFiles(token, { limit: 100 }),
    retry: (n, err) => !isDataPlaneDisabled(err) && n < 2,
  });

  // Every ready-made template opens JupyterLab, so none is offered while the browser cannot open one.
  const templates = useMemo(
    () => (!WEB_VIEW || templatesQuery.isError ? [] : templatesQuery.data ?? []),
    [templatesQuery.isError, templatesQuery.data],
  );
  const list = useMemo(() => machines.data?.items ?? [], [machines.data]);

  // The computer named in the link may not be in the list (offline, or it does not take rentals): look it up on its own.
  const named = useQuery({
    queryKey: ["rent", "machine", wantedMachine],
    enabled: Boolean(wantedMachine) && machines.isSuccess && !list.some((m) => m.machine_id === wantedMachine),
    queryFn: () => api.getMarketplaceMachine(wantedMachine as string),
    retry: false,
  });
  const pickable = useMemo(() => {
    const extra = named.data && named.data.container_rental ? [named.data] : [];
    return [...list, ...extra.filter((m) => !list.some((x) => x.machine_id === m.machine_id))];
  }, [list, named.data]);
  const linkProblem = Boolean(wantedMachine) && machines.isSuccess && !named.isLoading && !pickable.some((m) => m.machine_id === wantedMachine);

  // Start on the machine from the link once we know it can be chosen.
  useEffect(() => {
    if (wantedMachine && pickable.some((m) => m.machine_id === wantedMachine)) setForm((f) => (f.machineId ? f : { ...f, machineId: wantedMachine }));
  }, [wantedMachine, pickable]);

  // Without the web view only a terminal can be opened.
  useEffect(() => {
    if (!WEB_VIEW) setForm((f) => (f.appKind === "terminal" ? f : { ...f, appKind: "terminal" }));
  }, []);

  // Start on the first template, or on "my own image" when there are none.
  useEffect(() => {
    if (!WEB_VIEW) {
      setForm((f) => (f.source === "image" ? f : { ...f, source: "image", template: "" }));
      return;
    }
    if (!templatesQuery.isSuccess && !templatesQuery.isError) return;
    setForm((f) => {
      if (templates.length === 0) return f.source === "image" ? f : { ...f, source: "image", template: "" };
      return f.template ? f : { ...f, template: templates[0].slug };
    });
  }, [templatesQuery.isSuccess, templatesQuery.isError, templates]);

  const chosenMachine = pickable.find((m) => m.machine_id === form.machineId) ?? null;
  const chosenTemplate = templates.find((t) => t.slug === form.template) ?? null;
  const files = filesQuery.data ?? [];
  const dataOff = filesQuery.isError && isDataPlaneDisabled(filesQuery.error);
  const estimate = holdEstimate(form.maxPrice, form.gpuCount, form.maxMinutes);
  const tooLow = chosenMachine ? priceBelow(form.maxPrice, chosenMachine.price_per_hour) : false;

  const create = useMutation({
    mutationFn: async () => {
      const built = buildRentalRequest(form, templates);
      if (!built.ok) throw new Error("form_invalid");
      const key = newClientKey(); // the private half stays in this tab; only the public half is sent
      const s = await sessionsApi.create({ ...built.body, client_pubkey: key.pubHex });
      storeClientKey(s.id, key.priv);
      return s;
    },
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["sessions"] });
      router.push(`/dashboard/sessions/${s.id}`);
    },
    onError: (err) => {
      const code = apiErrorCode(err);
      const text = rentalErrorText(code);
      if (text) setServerError(text);
      else if (err instanceof ApiError && err.message && !/^[a-z0-9]+(_[a-z0-9]+)+$/.test(err.message)) setServerError(err.message);
      else setServerError("We could not start the rental. Check the details and try again in a moment.");
    },
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (create.isPending) return;
    setServerError(null);
    const built = buildRentalRequest(form, templates);
    if (!built.ok) {
      setErrors(built.errors);
      setInputRows(built.inputRows);
      return;
    }
    setErrors({});
    setInputRows({});
    create.mutate();
  }

  function toggleFile(f: DataFile, on: boolean) {
    setForm((cur) => ({
      ...cur,
      inputs: on ? [...cur.inputs, { fileId: f.id, path: suggestInputPath(f.name) }] : cur.inputs.filter((i) => i.fileId !== f.id),
    }));
  }
  function setInputPath(fileId: string, path: string) {
    setForm((cur) => ({ ...cur, inputs: cur.inputs.map((i) => (i.fileId === fileId ? { ...i, path } : i)) }));
  }
  function toggleEgress(id: string, on: boolean) {
    setForm((cur) => ({ ...cur, egress: on ? [...cur.egress, id] : cur.egress.filter((e) => e !== id) }));
  }

  const liveRows = inputProblems(form.inputs).rows;

  return (
    <>
      <ConsolePageHeader
        title="Rent a GPU"
        description="Pick a computer with a graphics card, then open JupyterLab or a terminal on it and run your own code, such as training or fine-tuning a model. You pay per second while it runs, and it stops by itself if you leave it idle."
        action={
          <Button asChild variant="outline">
            <Link href="/dashboard/sessions">Your sessions</Link>
          </Button>
        }
      />

      <form onSubmit={submit} noValidate className="space-y-6" aria-label="Rent a GPU">
        {/* 1. Which computer */}
        <Card>
          <CardContent className="space-y-3 p-6">
            <h2 className="text-sm font-semibold">1. Which computer</h2>
            {machines.isLoading ? (
              <p className="text-sm text-muted-foreground">Looking for computers that allow container rental…</p>
            ) : machines.isError ? (
              <p role="alert" className="text-sm text-danger">
                We could not load the list of computers. You can still start the rental on any available computer.
              </p>
            ) : pickable.length === 0 ? (
              <p className="flex items-start gap-2 text-sm text-muted-foreground">
                <Server aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                No computer allows container rental right now. You can still ask for one: your rental waits until a host that allows it is free.
              </p>
            ) : null}
            {linkProblem ? (
              <p role="status" className="rounded-md bg-surface-2 p-3 text-sm text-muted-foreground">
                The computer from the link is not taking container rentals right now. Choose another computer, or any available one.
              </p>
            ) : null}
            <div className="space-y-1.5">
              <Label htmlFor="machine">Computer</Label>
              <select
                id="machine"
                className={selectClass}
                value={form.machineId}
                onChange={(e) => set("machineId", e.target.value)}
                aria-describedby="machine-d"
                aria-invalid={Boolean(errors.machine)}
              >
                <option value="">Any computer that allows container rental</option>
                {pickable.map((m) => (
                  <option key={m.machine_id} value={m.machine_id}>
                    {machineLabel(m)}
                  </option>
                ))}
              </select>
              <p id="machine-d" className="text-xs text-muted-foreground">
                Only computers whose host chose to allow container rental are listed. Prices are in {CREDITS_LABEL}, not real money.
              </p>
              <FieldError id="machine-e" text={errors.machine} />
            </div>
            {tooLow && chosenMachine ? (
              <p role="status" className="text-sm text-warning">
                This computer asks {chosenMachine.price_per_hour} {CREDITS_LABEL} per hour, which is more than the most you said you will pay, so it will not pick up your rental.
                Raise the maximum price below, or choose another computer.
              </p>
            ) : null}
          </CardContent>
        </Card>

        {/* 2. What to run */}
        <Card>
          <CardContent className="space-y-4 p-6">
            <h2 className="text-sm font-semibold">2. What to run</h2>
            <fieldset className="space-y-2">
              <legend className="mb-1 text-sm font-medium">Start from</legend>
              <div className="flex flex-wrap gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="source"
                    checked={form.source === "template"}
                    disabled={templates.length === 0}
                    onChange={() => set("source", "template")}
                  />
                  A ready-made template
                </label>
                <label className="flex items-center gap-2">
                  <input type="radio" name="source" checked={form.source === "image"} onChange={() => set("source", "image")} />
                  My own image
                </label>
              </div>
            </fieldset>

            {!WEB_VIEW ? (
              <p role="status" className="rounded-md bg-surface-2 p-3 text-sm text-muted-foreground">
                Opening JupyterLab or another web page from a rental is not switched on for this site yet, so the ready-made templates are hidden and you can start a
                terminal on your own image. Ask the site&apos;s owner to switch on the session web address.
              </p>
            ) : null}
            {form.source === "template" ? (
              <div className="space-y-2">
                {templatesQuery.isLoading ? (
                  <p className="text-sm text-muted-foreground">Loading the templates…</p>
                ) : templates.length === 0 ? (
                  <p role="status" className="text-sm text-muted-foreground">
                    No ready-made templates are available yet. You can still bring your own image: choose &quot;My own image&quot; above.
                  </p>
                ) : (
                  <>
                    <Label htmlFor="template">Template</Label>
                    <select
                      id="template"
                      className={selectClass}
                      value={form.template}
                      onChange={(e) => set("template", e.target.value)}
                      aria-describedby="template-d"
                      aria-invalid={Boolean(errors.template)}
                    >
                      {templates.map((t) => (
                        <option key={t.slug} value={t.slug}>
                          {t.name}
                        </option>
                      ))}
                    </select>
                    <p id="template-d" className="text-xs text-muted-foreground">
                      {chosenTemplate?.description ?? "Pick the template you want to open."}
                    </p>
                    {chosenTemplate && chosenTemplate.libraries.length > 0 ? (
                      <p className="text-xs text-muted-foreground">Includes: {chosenTemplate.libraries.join(", ")}.</p>
                    ) : null}
                    <FieldError id="template-e" text={errors.template} />
                  </>
                )}
              </div>
            ) : (
              <div className="space-y-4">
                <div className="space-y-1.5">
                  <Label htmlFor="image">Image address</Label>
                  <Input
                    id="image"
                    value={form.image}
                    onChange={(e) => set("image", e.target.value)}
                    placeholder="ghcr.io/your-name/your-image@sha256:…"
                    autoComplete="off"
                    spellCheck={false}
                    aria-describedby="image-d image-e"
                    aria-invalid={Boolean(errors.image)}
                  />
                  <p id="image-d" className="text-xs text-muted-foreground">
                    A public image, pinned with @sha256: and its 64-character digest. Allowed places: Docker Hub (docker.io), ghcr.io, nvcr.io and quay.io. Anyone can pull a public
                    image, so do not put secrets or private models in it.
                  </p>
                  <FieldError id="image-e" text={errors.image} />
                </div>
                <fieldset className="space-y-2">
                  <legend className="mb-1 text-sm font-medium">How you want to use it</legend>
                  <div className="flex flex-wrap gap-4 text-sm">
                    <label className="flex items-center gap-2">
                      <input type="radio" name="appKind" checked={form.appKind === "terminal"} onChange={() => set("appKind", "terminal")} />
                      A terminal (command window)
                    </label>
                    <label className="flex items-center gap-2">
                      <input type="radio" name="appKind" checked={form.appKind === "web"} disabled={!WEB_VIEW} onChange={() => set("appKind", "web")} />
                      A program with a web page, such as Jupyter
                    </label>
                  </div>
                </fieldset>
                {form.appKind === "web" ? (
                  <div className="space-y-1.5">
                    <Label htmlFor="port">Port the program listens on</Label>
                    <Input id="port" inputMode="numeric" value={form.port} onChange={(e) => set("port", e.target.value)} placeholder="8888" aria-invalid={Boolean(errors.port)} aria-describedby="port-e" />
                    <FieldError id="port-e" text={errors.port} />
                  </div>
                ) : null}
                <div className="space-y-1.5">
                  <Label htmlFor="command">Start command (optional)</Label>
                  <Input
                    id="command"
                    value={form.command}
                    onChange={(e) => set("command", e.target.value)}
                    placeholder={form.appKind === "web" ? "jupyter lab --ip=127.0.0.1 --port=8888" : "/bin/bash"}
                    autoComplete="off"
                    spellCheck={false}
                    aria-describedby="command-d command-e"
                    aria-invalid={Boolean(errors.command)}
                  />
                  <p id="command-d" className="text-xs text-muted-foreground">
                    Plain words separated by spaces. Leave it empty to use the image&apos;s own start-up. Quotes are not supported.
                  </p>
                  <FieldError id="command-e" text={errors.command} />
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* 3. Size */}
        <Card>
          <CardContent className="space-y-4 p-6">
            <h2 className="text-sm font-semibold">3. How much computer you need</h2>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <div className="space-y-1.5">
                <Label htmlFor="gpus">Graphics cards</Label>
                <select id="gpus" className={selectClass} value={form.gpuCount} onChange={(e) => set("gpuCount", e.target.value)} aria-invalid={Boolean(errors.gpuCount)}>
                  {Array.from({ length: RENTAL_LIMITS.maxGpus }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={String(n)}>
                      {n}
                    </option>
                  ))}
                </select>
                <FieldError id="gpus-e" text={errors.gpuCount} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="mem">Memory (GB)</Label>
                <Input id="mem" inputMode="numeric" value={form.memoryGb} onChange={(e) => set("memoryGb", e.target.value)} aria-invalid={Boolean(errors.memoryGb)} aria-describedby="mem-e" />
                <FieldError id="mem-e" text={errors.memoryGb} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="cpus">Processor cores</Label>
                <Input id="cpus" inputMode="numeric" value={form.cpus} onChange={(e) => set("cpus", e.target.value)} aria-invalid={Boolean(errors.cpus)} aria-describedby="cpus-e" />
                <FieldError id="cpus-e" text={errors.cpus} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="ws">Workspace size (GB)</Label>
                <Input id="ws" inputMode="numeric" value={form.workspaceGb} onChange={(e) => set("workspaceGb", e.target.value)} aria-invalid={Boolean(errors.workspaceGb)} aria-describedby="ws-d ws-e" />
                <p id="ws-d" className="text-xs text-muted-foreground">Your folder on the host&apos;s disk. It is deleted when the rental ends.</p>
                <FieldError id="ws-e" text={errors.workspaceGb} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="price">Most you will pay per graphics card per hour ({CREDITS_LABEL})</Label>
                <Input id="price" inputMode="decimal" value={form.maxPrice} onChange={(e) => set("maxPrice", e.target.value)} aria-invalid={Boolean(errors.maxPrice)} aria-describedby="price-d price-e" />
                <p id="price-d" className="text-xs text-muted-foreground">We set aside enough for the longest run and give back what you do not use.</p>
                <FieldError id="price-e" text={errors.maxPrice} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="maxmin">Longest it can run (minutes)</Label>
                <Input id="maxmin" inputMode="numeric" value={form.maxMinutes} onChange={(e) => set("maxMinutes", e.target.value)} aria-invalid={Boolean(errors.maxMinutes)} aria-describedby="maxmin-e" />
                <FieldError id="maxmin-e" text={errors.maxMinutes} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="idle">Stop if idle for (minutes)</Label>
                <Input id="idle" inputMode="numeric" value={form.idleMinutes} onChange={(e) => set("idleMinutes", e.target.value)} aria-invalid={Boolean(errors.idleMinutes)} aria-describedby="idle-e" />
                <FieldError id="idle-e" text={errors.idleMinutes} />
              </div>
            </div>
            {estimate ? (
              <p className="text-sm text-muted-foreground" role="status">
                If it runs for the whole time, we set aside up to about {estimate} {CREDITS_LABEL} plus the platform fee, and give back what you do not use.
              </p>
            ) : null}
          </CardContent>
        </Card>

        {/* 4. Files in */}
        <Card>
          <CardContent className="space-y-3 p-6">
            <h2 className="text-sm font-semibold">4. Files to bring in (optional)</h2>
            {dataOff ? (
              <p className="text-sm text-muted-foreground">File storage is not switched on yet, so you cannot bring files in from your storage.</p>
            ) : filesQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">Loading your files…</p>
            ) : filesQuery.isError ? (
              <p role="alert" className="text-sm text-danger">We could not load your files. You can still start the rental without them.</p>
            ) : files.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                You have no stored files yet. Upload a dataset on the Kits page first, and it will appear here. You can also download data from inside the rental if you allow the internet choices below.
              </p>
            ) : (
              <>
                <p className="text-xs text-muted-foreground">
                  Chosen files are copied into your workspace before the rental starts. That time is not charged. At most {RENTAL_LIMITS.maxInputs} files.
                </p>
                <ul className="space-y-2">
                  {files.map((f) => {
                    const row = form.inputs.find((i) => i.fileId === f.id);
                    const idx = row ? form.inputs.indexOf(row) : -1;
                    const problem = idx >= 0 ? inputRows[idx] ?? liveRows[idx] : undefined;
                    return (
                      <li key={f.id} className="rounded-md border border-border p-3">
                        <label className="flex flex-wrap items-center gap-2 text-sm">
                          <input type="checkbox" checked={Boolean(row)} onChange={(e) => toggleFile(f, e.target.checked)} aria-label={`Bring in ${f.name}`} />
                          <span className="break-all font-medium">{f.name}</span>
                          <span className="text-xs text-muted-foreground">{formatBytes(f.size_bytes)}</span>
                        </label>
                        {row ? (
                          <div className="mt-2 space-y-1">
                            <Label htmlFor={`path-${f.id}`} className="text-xs">
                              Place it at (inside the workspace)
                            </Label>
                            <Input
                              id={`path-${f.id}`}
                              value={row.path}
                              onChange={(e) => setInputPath(f.id, e.target.value)}
                              autoComplete="off"
                              spellCheck={false}
                              aria-invalid={Boolean(problem)}
                              aria-describedby={`path-${f.id}-e`}
                            />
                            <FieldError id={`path-${f.id}-e`} text={problem} />
                          </div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
                <FieldError id="inputs-e" text={errors.inputs} />
              </>
            )}
          </CardContent>
        </Card>

        {/* 5. Network */}
        <Card>
          <CardContent className="space-y-3 p-6">
            <h2 className="text-sm font-semibold">5. Internet access (optional)</h2>
            <p className="text-xs text-muted-foreground">
              By default your rental has no internet at all. Tick only what you need, for example pip to install libraries. Each choice allows only the listed websites.
            </p>
            <ul className="grid gap-2 sm:grid-cols-2">
              {EGRESS_CHOICES.map((c) => (
                <li key={c.id} className="rounded-md border border-border p-3">
                  <label className="flex items-start gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={form.egress.includes(c.id)}
                      onChange={(e) => toggleEgress(c.id, e.target.checked)}
                    />
                    <span>
                      <span className="font-medium">{c.label}</span>
                      <span className="block text-xs text-muted-foreground">{c.description}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            {chosenTemplate && chosenTemplate.default_egress.length > 0 ? (
              <p className="text-xs text-muted-foreground">This template already allows: {chosenTemplate.default_egress.join(", ")}.</p>
            ) : null}
          </CardContent>
        </Card>

        {/* 6. The warning */}
        <Card className="border-warning/50">
          <CardContent className="space-y-3 p-6">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <AlertTriangle aria-hidden="true" className="h-4 w-4 text-warning" /> Before you start: please read
            </h2>
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {RENTAL_WARNING_POINTS.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
            <label className="flex items-start gap-2 text-sm font-medium">
              <input
                type="checkbox"
                className="mt-1"
                checked={form.accepted}
                onChange={(e) => set("accepted", e.target.checked)}
                aria-describedby="accepted-e"
                aria-invalid={Boolean(errors.accepted)}
              />
              <span>{RENTAL_ACCEPT_LABEL}</span>
            </label>
            <FieldError id="accepted-e" text={errors.accepted} />
          </CardContent>
        </Card>

        {serverError ? (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
            {serverError}
          </p>
        ) : null}
        {Object.keys(errors).length > 0 ? (
          <p role="alert" className="text-sm text-danger">
            Some answers need fixing. They are marked above.
          </p>
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={create.isPending || !form.accepted}>
            {create.isPending ? <Loader2 className="animate-spin" /> : null}
            {create.isPending ? "Starting…" : "Start rental"}
          </Button>
          {!form.accepted ? <Badge variant="neutral">Tick the box above to continue</Badge> : null}
        </div>
      </form>
    </>
  );
}
