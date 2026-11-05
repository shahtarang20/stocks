"use client";

import { Suspense, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, ArrowRight, Info, Loader2, Rocket } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { SchemaForm } from "@/components/kits/schema-form";
import { FileUpload } from "@/components/kits/file-upload";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import {
  estimateMinutes, formatDuration, kitsApi, kitSubmitErrorMessage, tryQuote, verificationLabel,
  type DataFile, type JobSpecV2, type KitDetail, type KitQuote,
} from "@/lib/kits";
import {
  buildParams, extractServerErrors, fieldsFromSchema, initialValues, mapServerErrors,
  type FieldErrors, type RawValues, type ServerFieldError,
} from "@/lib/schema-form";
import { formatBytes } from "@/lib/utils";

const STEPS = ["Settings", "Files", "Review and start"] as const;

function Wizard() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const params = useParams<{ slug: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const slug = params?.slug ?? "";
  const version = search?.get("v") ?? "";

  const versionsQuery = useQuery({
    queryKey: ["kit-versions", slug],
    enabled: Boolean(slug) && !version,
    queryFn: () => kitsApi.listVersions(slug),
  });
  const resolvedVersion = version || versionsQuery.data?.find((k) => k.status === "published")?.version || "";
  const kitQuery = useQuery<KitDetail>({
    queryKey: ["kit", slug, resolvedVersion],
    enabled: Boolean(slug) && Boolean(resolvedVersion),
    queryFn: () => kitsApi.getKit(slug, resolvedVersion),
  });
  const kit = kitQuery.data;

  const fields = useMemo(() => fieldsFromSchema(kit?.manifest.param_schema), [kit]);
  const [step, setStep] = useState(0);
  const [raw, setRaw] = useState<RawValues | null>(null);
  const values = raw ?? initialValues(fields);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [general, setGeneral] = useState<ServerFieldError[]>([]);
  const [submitMessage, setSubmitMessage] = useState<string | null>(null);
  const [files, setFiles] = useState<Record<string, DataFile | undefined>>({});
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});
  const [units, setUnits] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [quote, setQuote] = useState<KitQuote | null>(null);
  const [quoteTried, setQuoteTried] = useState(false);
  const idemKey = useRef<string>(typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now()));

  const manifest = kit?.manifest;
  const kitInputs = manifest?.inputs ?? [];
  const unitLabel = manifest?.billing_hints?.unit ?? "units";
  const unitsNum = units.trim() ? Number(units) : NaN;
  const gpuCount = manifest?.resource_defaults?.gpu_count ?? 1;
  const localMinutes = manifest ? estimateMinutes(manifest, unitsNum, gpuCount) : null;

  function setValue(name: string, v: string | boolean) {
    setRaw({ ...values, [name]: v });
    if (fieldErrors[name]) setFieldErrors({ ...fieldErrors, [name]: "" });
  }

  function goSettingsNext() {
    const { errors } = buildParams(fields, values);
    setFieldErrors(errors);
    if (Object.keys(errors).length === 0) setStep(1);
  }

  function goFilesNext() {
    const errs: Record<string, string> = {};
    for (const i of kitInputs) if (i.required !== false && !files[i.name]) errs[i.name] = "Upload a file for this input.";
    setInputErrors(errs);
    if (Object.keys(errs).length === 0) setStep(2);
  }

  function buildSpec(): JobSpecV2 | null {
    if (!kit || !manifest) return null;
    const { params: p, errors } = buildParams(fields, values);
    if (Object.keys(errors).length) return null;
    const spec: JobSpecV2 = {
      version: 2,
      kit: `${kit.slug}@${kit.version}`,
      mode: "batch",
      inputs: kitInputs.filter((i) => files[i.name]).map((i) => ({ name: i.name, source: `dataset:${files[i.name]!.id}` })),
      outputs: (manifest.outputs ?? []).map((o) => ({ name: o.name, stream_previews: true })),
      params: p,
    };
    const billing: NonNullable<JobSpecV2["billing"]> = {};
    if (manifest.billing_hints?.unit) billing.unit = manifest.billing_hints.unit;
    // The API takes whole units (integer >= 1); a fractional value would be a 422 on submit.
    if (Number.isFinite(unitsNum) && unitsNum > 0) billing.expected_units = Math.ceil(unitsNum);
    if (maxCost.trim()) billing.max_cost = maxCost.trim();
    if (Object.keys(billing).length) spec.billing = billing;
    return spec;
  }

  // Quote (optional): needs units and a maximum cost; absence (feature off) is normal.
  async function refreshQuote() {
    setQuoteTried(true);
    if (!kit || !Number.isFinite(unitsNum) || unitsNum <= 0 || !maxCost.trim()) {
      setQuote(null);
      return;
    }
    setQuote(
      await tryQuote(token, {
        kit: `${kit.slug}@${kit.version}`, expected_units: Math.ceil(unitsNum), max_cost: maxCost.trim(), gpu_count: gpuCount,
      }),
    );
  }

  const submit = useMutation({
    mutationFn: async () => {
      const spec = buildSpec();
      if (!spec) throw new Error("invalid_form");
      return kitsApi.createJobV2(
        token,
        { spec, isolation_tier: "container" },
        idemKey.current,
      );
    },
    onSuccess: (job) => router.push(`/dashboard/jobs/${job.job_id ?? job.id}`),
    onError: (err) => {
      // Keep the Idempotency-Key when the outcome is unknown (network failure or 5xx): the server may have
      // created the job, and a retry with the same key returns it instead of starting (and billing) a second one.
      // A definite rejection (4xx) gets a fresh key because the spec may change before the next attempt.
      const outcomeUnknown = err instanceof ApiError && (err.status === 0 || err.status >= 500);
      if (!outcomeUnknown) {
        idemKey.current = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now());
      }
      if (err instanceof ApiError) {
        const server = extractServerErrors(err);
        const mapped = mapServerErrors(server, fields);
        setFieldErrors(mapped.fieldErrors);
        setGeneral(mapped.general);
        setSubmitMessage(kitSubmitErrorMessage(err));
        if (Object.keys(mapped.fieldErrors).length) setStep(0);
        else if (mapped.general.some((e) => e.field.startsWith("inputs"))) setStep(1);
      } else {
        setSubmitMessage("Something is not filled in right. Check the form and try again.");
      }
    },
  });

  if (kitQuery.isLoading || versionsQuery.isLoading) {
    return (
      <>
        <ConsolePageHeader title="Start a job" />
        <Card><CardContent className="space-y-4 p-6" aria-busy="true"><Skeleton className="h-6 w-48" /><Skeleton className="h-32 w-full" /></CardContent></Card>
      </>
    );
  }
  if (kitQuery.isError || !kit || !manifest) {
    const notFound = kitQuery.error instanceof ApiError && kitQuery.error.status === 404;
    return (
      <>
        <ConsolePageHeader title="Start a job" />
        <Card className="border-danger/40">
          <CardContent className="flex items-start gap-3 p-6" role="alert">
            <AlertCircle aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
            <div>
              <h2 className="font-semibold">{notFound ? "We could not find this Kit" : "We could not load this Kit"}</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {notFound ? "It may have been removed. Go back and pick another Kit." : "Check your internet connection and try again."}
              </p>
              <Button asChild className="mt-3" variant="outline" size="sm">
                <Link href="/dashboard/kits"><ArrowLeft /> Back to Kits</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      </>
    );
  }

  if (manifest.mode && manifest.mode !== "batch") {
    // Opened by link: a session or service Kit is refused by POST /v1/jobs/v2, so say so up front instead of after the form.
    const isSession = manifest.mode === "session";
    return (
      <>
        <ConsolePageHeader title={manifest.name} description={manifest.description ?? undefined} />
        <Card>
          <CardContent className="flex items-start gap-3 p-6" role="status">
            <Info aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
            <div>
              <h2 className="font-semibold">{isSession ? "This Kit opens as a session, not a job" : "This Kit runs as a service"}</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {isSession
                  ? "It gives you a live terminal on a host's computer. Start it from the Sessions page."
                  : "It stays running and answers requests. It is started through the API, not from this form."}
              </p>
              <Button asChild className="mt-3" variant="outline" size="sm">
                {isSession ? (
                  <Link href={`/dashboard/sessions?kit=${encodeURIComponent(`${kit.slug}@${kit.version}`)}`}>Open Sessions <ArrowRight /></Link>
                ) : (
                  <Link href="/dashboard/kits"><ArrowLeft /> Back to Kits</Link>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>
      </>
    );
  }

  // On the Files step input-level errors already show next to their input; do not announce them twice.
  const bannerErrors = step === 1 ? general.filter((e) => !e.field.startsWith("inputs[")) : general;
  const stepErrors = Object.values(fieldErrors).filter(Boolean).length;

  return (
    <>
      <ConsolePageHeader
        title={`Start a job: ${manifest.name}`}
        description={manifest.description ?? "Answer a few questions, upload your files and start."}
        action={
          <Button asChild variant="outline">
            <Link href="/dashboard/kits"><ArrowLeft /> Kits</Link>
          </Button>
        }
      />

      <nav aria-label="Progress" className="mb-6">
        <ol className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
          {STEPS.map((s, i) => (
            <li
              key={s}
              aria-current={i === step ? "step" : undefined}
              className={i === step ? "font-semibold text-foreground" : "text-muted-foreground"}
            >
              {i + 1}. {s}
            </li>
          ))}
        </ol>
      </nav>

      {submitMessage || general.length ? (
        <div role="alert" className="mb-6 rounded-md border border-danger/40 bg-danger/10 px-4 py-3 text-sm">
          <p className="font-medium">{submitMessage ?? "A few answers need a fix."}</p>
          {bannerErrors.length ? (
            <ul className="mt-1 list-disc pl-5 text-muted-foreground">
              {bannerErrors.map((e, i) => (<li key={i}>{e.message}</li>))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <Card>
        <CardContent className="p-4 sm:p-6">
          {step === 0 ? (
            <form
              onSubmit={(e) => { e.preventDefault(); goSettingsNext(); }}
              noValidate
              aria-label="Settings"
              className="space-y-6"
            >
              <h2 className="text-lg font-semibold">Settings</h2>
              {stepErrors ? <p role="alert" className="text-sm text-danger">Fix the settings marked in red to continue.</p> : null}
              <SchemaForm fields={fields} values={values} errors={fieldErrors} onChange={setValue} />
              <div className="flex justify-end">
                <Button type="submit" className="w-full sm:w-auto">Next: files <ArrowRight /></Button>
              </div>
            </form>
          ) : null}

          {step === 1 ? (
            <form onSubmit={(e) => { e.preventDefault(); goFilesNext(); }} aria-label="Files" className="space-y-6">
              <h2 className="text-lg font-semibold">Your files</h2>
              {kitInputs.length === 0 ? (
                <p className="text-sm text-muted-foreground">This Kit does not need any files. Press Next to go on.</p>
              ) : null}
              {kitInputs.map((i) => (
                <FileUpload
                  key={i.name}
                  label={humanizeInput(i.name)}
                  description={i.kind ? `Kind of file: ${i.kind}` : undefined}
                  required={i.required !== false}
                  token={token}
                  error={inputErrors[i.name] || serverInputError(general, i.name, kitInputs.filter((x) => files[x.name]).map((x) => x.name))}
                  onUploaded={(f) => { setFiles((cur) => ({ ...cur, [i.name]: f })); setInputErrors((e) => ({ ...e, [i.name]: "" })); }}
                  onCleared={() => setFiles((cur) => ({ ...cur, [i.name]: undefined }))}
                />
              ))}
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-between">
                <Button type="button" variant="outline" onClick={() => setStep(0)}><ArrowLeft /> Back</Button>
                <Button type="submit">Next: review <ArrowRight /></Button>
              </div>
            </form>
          ) : null}

          {step === 2 ? (
            <form
              onSubmit={(e) => { e.preventDefault(); setSubmitMessage(null); setGeneral([]); submit.mutate(); }}
              aria-label="Review and start"
              className="space-y-6"
            >
              <h2 className="text-lg font-semibold">Price and time</h2>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="units">How many {unitLabel}? (optional)</Label>
                  <Input id="units" inputMode="numeric" value={units} onChange={(e) => setUnits(e.target.value)} aria-describedby="units-d" />
                  <p id="units-d" className="text-xs text-muted-foreground">Your best guess of how much you will send. We use it to estimate the time.</p>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="maxcost">Most you want to spend (optional)</Label>
                  <Input id="maxcost" inputMode="decimal" value={maxCost} onChange={(e) => setMaxCost(e.target.value)} aria-describedby="maxcost-d" />
                  <p id="maxcost-d" className="text-xs text-muted-foreground">The job will never cost more than this many credits.</p>
                </div>
              </div>

              <div className="rounded-md border border-border bg-surface-2 p-4 text-sm" aria-live="polite">
                <p className="flex items-center gap-2 font-medium"><Info aria-hidden="true" className="h-4 w-4" /> Estimate</p>
                <p className="mt-1 text-muted-foreground">
                  {quote
                    ? `Quoted total ${quote.total ?? "n/a"} ${quote.currency === "USDC_CREDITS" || !quote.currency ? "credits" : quote.currency} expected (never more than your maximum).`
                    : localMinutes
                      ? `Time: ${formatDuration(localMinutes)} (the Kit's own estimate).`
                      : "Fill in how many above to see a time estimate."}
                  {" "}
                  {quote
                    ? ""
                    : quoteTried
                      ? "A live quote is not available right now. The final price is set when a host's computer is picked, and it never goes above your maximum."
                      : "The final price is set when a host's computer is picked, and it never goes above your maximum."}
                </p>
              </div>

              <Button type="button" variant="outline" onClick={() => void refreshQuote()}>Get a price estimate</Button>

              <h2 className="text-lg font-semibold">Summary</h2>
              <dl className="grid gap-3 text-sm sm:grid-cols-2">
                <div><dt className="text-muted-foreground">Kit</dt><dd className="font-medium">{kit.name} v{kit.version}</dd></div>
                <div><dt className="text-muted-foreground">How the result is checked</dt><dd className="font-medium">{verificationLabel(manifest.verification?.method)}</dd></div>
                {kitInputs.map((i) => (
                  <div key={i.name}><dt className="text-muted-foreground">{humanizeInput(i.name)}</dt><dd className="font-medium break-words">{files[i.name] ? `${files[i.name]!.name} (${formatBytes(files[i.name]!.size_bytes)})` : "no file"}</dd></div>
                ))}
              </dl>

              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-between">
                <Button type="button" variant="outline" onClick={() => setStep(1)}><ArrowLeft /> Back</Button>
                <Button type="submit" disabled={submit.isPending}>
                  {submit.isPending ? <Loader2 className="animate-spin" /> : <Rocket />} Start job
                </Button>
              </div>
            </form>
          ) : null}
        </CardContent>
      </Card>
    </>
  );
}

function humanizeInput(name: string): string {
  const s = name.replace(/[_-]+/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Server errors like `inputs[0].source` come back by index; show them on the matching input. */
function serverInputError(general: ServerFieldError[], name: string, names: string[]): string | undefined {
  const idx = names.indexOf(name);
  if (idx < 0) return undefined;
  return general.find((e) => e.field.startsWith(`inputs[${idx}]`))?.message;
}

export default function KitWizardPage() {
  return (
    <Suspense fallback={null}>
      <Wizard />
    </Suspense>
  );
}
