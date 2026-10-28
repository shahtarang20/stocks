"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, Download, Info, Key, Loader2, Lock } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  CREDITS_LABEL,
  type CreateJobInput,
  type Job,
  privacyApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { createJobErrorMessage } from "@/lib/jobs";
import { checkGuardianPin, preparePrivateJob } from "@/lib/ccpp";

// The guardian-set fingerprint ComputeCoin publishes outside this website (spec 6e). Set at build time.
const GUARDIAN_PIN = process.env.NEXT_PUBLIC_PRIVACY_GUARDIAN_PIN;
const REQUIRE_GUARDIAN_PIN = process.env.NEXT_PUBLIC_PRIVACY_REQUIRE_GUARDIAN_PIN === "1";
import {
  getOrGenerateCustomerKey,
  markCustomerKeyBackedUp,
  exportCustomerKeyBackupJson,
  type StoredCustomerKey,
} from "@/lib/customer-keys";

const selectClass =
  "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

const textareaClass =
  "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background aria-[invalid=true]:border-danger";

const decimal = (v: string) => /^\d+(\.\d{1,2})?$/.test(v.trim());

const schema = z
  .object({
    workload_type: z.enum(["text-generation", "batch-inference"]),
    isolation_tier: z
      .enum([
        "open",
        "sealed",
        "sealed_pro",
        "confidential",
        "container",
        "gvisor",
        "microvm",
      ])
      .default("container"),
    required_gpu_runtime: z.enum(["cuda", "rocm", "any"]).default("cuda"),
    model: z.string().trim().min(1, "Model is required"),
    prompt: z.string().optional(),
    max_tokens: z
      .string()
      .optional()
      .refine((v) => !v || /^\d+$/.test(v.trim()), {
        message: "Enter a whole number of tokens",
      }),
    items_json: z.string().optional(),
    machine_id: z.string().optional(),
    gpu_count: z.coerce
      .number({ invalid_type_error: "Enter a whole number" })
      .int("Whole number")
      .min(1, "At least 1"),
    estimated_minutes: z.coerce
      .number({ invalid_type_error: "Enter a whole number" })
      .int("Whole number")
      .min(1, "At least 1 minute"),
    max_cost: z
      .string()
      .trim()
      .refine(decimal, { message: "Enter a decimal like 5.00" }),
    max_runtime_seconds: z.coerce
      .number({ invalid_type_error: "Enter a whole number" })
      .int("Whole number")
      .min(1, "At least 1 second"),
    max_retries: z.coerce
      .number({ invalid_type_error: "Enter a whole number" })
      .int("Whole number")
      .min(0, "Cannot be negative")
      .max(10, "Too many"),
  })
  .superRefine((val, ctx) => {
    if (val.workload_type === "text-generation") {
      if (!val.prompt || val.prompt.trim().length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["prompt"],
          message: "A prompt is required for text-generation",
        });
      }
    } else {
      // batch-inference — require a JSON object with a non-empty items array.
      const raw = (val.items_json ?? "").trim();
      if (!raw) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["items_json"],
          message: "Provide a JSON object with an items array",
        });
        return;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["items_json"],
          message: "Invalid JSON",
        });
        return;
      }
      const items =
        parsed && typeof parsed === "object"
          ? (parsed as { items?: unknown }).items
          : undefined;
      if (!Array.isArray(items) || items.length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["items_json"],
          message: 'Must be {"items": [ ... ]} with at least one item',
        });
      }
    }
    if (val.required_gpu_runtime === "rocm" && val.isolation_tier !== "container") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["isolation_tier"],
        message: "AMD (ROCm) jobs can only use the Container isolation tier for now.",
      });
    }
  });

type FormValues = z.input<typeof schema>;

export default function NewJobPage() {
  const { accessToken } = useAuth();
  const router = useRouter();

  const [backupModalOpen, setBackupModalOpen] = useState(false);
  const [pendingValues, setPendingValues] = useState<FormValues | null>(null);
  const [customerKey, setCustomerKey] = useState<StoredCustomerKey | null>(null);
  const [prepError, setPrepError] = useState<string | null>(null);
  const [isPreparing, setIsPreparing] = useState(false);

  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      workload_type: "text-generation",
      isolation_tier: "container",
      required_gpu_runtime: "cuda",
      model: "echo-sim-v1",
      prompt: "",
      max_tokens: "64",
      items_json: '{\n  "items": [\n    { "prompt": "Hello" }\n  ]\n}',
      machine_id: "",
      gpu_count: 1,
      estimated_minutes: 5,
      max_cost: "5.00",
      max_runtime_seconds: 300,
      max_retries: 1,
    },
  });

  const workloadType = watch("workload_type");
  const isolationTier = watch("isolation_tier");
  const requiredGpuRuntime = watch("required_gpu_runtime");
  const isPrivateTier = Boolean(
    isolationTier && ["sealed", "sealed_pro", "confidential"].includes(isolationTier)
  );

  useEffect(() => {
    if (requiredGpuRuntime === "rocm" && isolationTier !== "container") {
      setValue("isolation_tier", "container");
    }
  }, [requiredGpuRuntime, isolationTier, setValue]);

  const createMut = useMutation({
    mutationFn: (input: CreateJobInput) =>
      // Idempotency-Key makes a retried submit return the SAME job (no duplicate).
      api.createJob(accessToken as string, input, crypto.randomUUID()),
    onSuccess: (job: Job) => {
      const id = job.job_id ?? job.id;
      router.push(id ? `/dashboard/jobs/${id}` : "/dashboard/jobs");
    },
  });

  async function executeJobSubmission(values: FormValues, key: StoredCustomerKey | null) {
    let input: Record<string, unknown>;
    if (values.workload_type === "text-generation") {
      input = { prompt: (values.prompt ?? "").trim() };
      const mt = (values.max_tokens ?? "").trim();
      if (mt) input.max_tokens = Number(mt);
    } else {
      input = JSON.parse((values.items_json ?? "").trim());
    }

    const payload: CreateJobInput = {
      workload_type: values.workload_type,
      isolation_tier: values.isolation_tier,
      required_gpu_runtime: values.required_gpu_runtime,
      model: values.model.trim(),
      input,
      gpu_count: Number(values.gpu_count),
      estimated_minutes: Number(values.estimated_minutes),
      max_cost: String(values.max_cost).trim(),
      max_runtime_seconds: Number(values.max_runtime_seconds),
      retry_policy: { max_retries: Number(values.max_retries) },
    };
    const machine = (values.machine_id ?? "").trim();
    if (machine) payload.machine_id = machine;

    if (
      values.isolation_tier &&
      ["sealed", "sealed_pro", "confidential"].includes(values.isolation_tier)
    ) {
      if (!key) {
        setPrepError("No customer encryption key available");
        return;
      }
      setIsPreparing(true);
      setPrepError(null);
      try {
        const verifier = await privacyApi.getVerifierKeys();
        // Refuse a guardian set that does not match the fingerprint published out of band.
        checkGuardianPin(
          verifier.guardians,
          verifier.threshold,
          GUARDIAN_PIN,
          REQUIRE_GUARDIAN_PIN
        );
        const prep = preparePrivateJob(
          input,
          verifier.guardians,
          verifier.threshold,
          key.publicKeyHex
        );
        payload.input = {}; // Cleaned: plaintext is never sent to the API
        payload.encrypted_input = prep.encrypted_input as any;
        payload.dk_shares = prep.dk_shares as any;
        payload.data_policy = {
          ...(payload.data_policy || {}),
          result_public_key: key.publicKeyHex,
        };
      } catch (err: any) {
        setIsPreparing(false);
        setPrepError("Failed to prepare private job encryption: " + (err?.message || String(err)));
        return;
      }
      setIsPreparing(false);
    }

    createMut.mutate(payload);
  }

  async function onSubmit(values: FormValues) {
    setPrepError(null);
    if (
      values.isolation_tier &&
      ["sealed", "sealed_pro", "confidential"].includes(values.isolation_tier)
    ) {
      const key = await getOrGenerateCustomerKey();
      setCustomerKey(key);
      if (!key.backedUp) {
        setPendingValues(values);
        setBackupModalOpen(true);
        return;
      }
      await executeJobSubmission(values, key);
    } else {
      await executeJobSubmission(values, null);
    }
  }

  async function handleDownloadKeyBackup() {
    try {
      const jsonStr = await exportCustomerKeyBackupJson();
      const blob = new Blob([jsonStr], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "computecoin-private-key-backup.json";
      a.click();
      URL.revokeObjectURL(url);

      await markCustomerKeyBackedUp();
      const updatedKey = customerKey ? { ...customerKey, backedUp: true } : null;
      setCustomerKey(updatedKey);
      setBackupModalOpen(false);

      if (pendingValues && updatedKey) {
        const toSubmit = pendingValues;
        setPendingValues(null);
        await executeJobSubmission(toSubmit, updatedKey);
      }
    } catch (err: any) {
      setPrepError("Key backup failed: " + (err?.message || String(err)));
    }
  }

  return (
    <>
      <ConsolePageHeader
        title="New job"
        description="Submit a compute job via POST /v1/jobs. The scheduler validates, matches a machine, reserves capacity and dispatches it to the provider agent."
        action={
          <Button asChild variant="outline">
            <Link href="/dashboard/jobs">
              <ArrowLeft /> Back to jobs
            </Link>
          </Button>
        }
      />

      <div className="mb-6 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
        <p className="text-muted-foreground">
          <span className="font-medium text-foreground">
            CPU-only infrastructure runs in{" "}
            <span className="font-mono">simulated</span> execution mode.
          </span>{" "}
          A simulated job returns a deterministic transform of your input — it is{" "}
          <span className="font-medium">not real model inference</span> and must
          never be treated as such. Nothing is charged in Phase 4.
        </p>
      </div>

      <Card>
        <CardContent className="p-6">
          <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-5">
            {createMut.isError || prepError ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{prepError || createJobErrorMessage(createMut.error)}</span>
              </div>
            ) : null}

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="space-y-1.5">
                <Label htmlFor="workload_type">Workload type</Label>
                <select
                  id="workload_type"
                  className={selectClass}
                  {...register("workload_type")}
                >
                  <option value="text-generation">text-generation</option>
                  <option value="batch-inference">batch-inference</option>
                </select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="required_gpu_runtime">GPU runtime</Label>
                <select
                  id="required_gpu_runtime"
                  className={selectClass}
                  {...register("required_gpu_runtime")}
                >
                  <option value="cuda">CUDA</option>
                  <option value="rocm">ROCm</option>
                  <option value="any">Any</option>
                </select>
                <p className="mt-1 text-xs text-muted-foreground">
                  {requiredGpuRuntime === "rocm"
                    ? "AMD (ROCm) GPUs: Container isolation only for now."
                    : "Target GPU runtime environment."}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="isolation_tier">Isolation & Privacy Tier</Label>
                <select
                  id="isolation_tier"
                  className={selectClass}
                  {...register("isolation_tier")}
                >
                  <option value="container">Container (shared kernel)</option>
                  <option value="gvisor" disabled={requiredGpuRuntime === "rocm"}>
                    gVisor sandbox (runsc){requiredGpuRuntime === "rocm" ? " (container only on ROCm)" : ""}
                  </option>
                  <option value="microvm" disabled={requiredGpuRuntime === "rocm"}>
                    microVM (Kata / Firecracker){requiredGpuRuntime === "rocm" ? " (container only on ROCm)" : ""}
                  </option>
                  <option value="sealed" disabled={requiredGpuRuntime === "rocm"}>
                    Sealed (private){requiredGpuRuntime === "rocm" ? " (container only on ROCm)" : ""}
                  </option>
                  <option value="sealed_pro" disabled={requiredGpuRuntime === "rocm"}>
                    Sealed Pro (private){requiredGpuRuntime === "rocm" ? " (container only on ROCm)" : ""}
                  </option>
                  <option value="confidential" disabled={requiredGpuRuntime === "rocm"}>
                    Confidential (private){requiredGpuRuntime === "rocm" ? " (container only on ROCm)" : ""}
                  </option>
                </select>
                <p className="mt-1 text-xs text-muted-foreground">
                  {isPrivateTier ? (
                    <span className="text-emerald-400">
                      Your input is encrypted in this browser and only your browser can decrypt the
                      result; ComputeCoin&apos;s servers never see either. The host that runs the job
                      decrypts the input in memory to compute on it. Hardware attestation (TPM / TEE)
                      is not live yet, so today this relies on the host running the published sealed
                      image and on its bonded stake, not on a hardware proof.
                      {GUARDIAN_PIN ? null : (
                        <>
                          {" "}This build does not pin the guardian keys, so you are trusting ComputeCoin
                          to serve the right ones.
                        </>
                      )}
                    </span>
                  ) : isolationTier === "gvisor" || isolationTier === "microvm" ? (
                    <span>Stronger sandboxing against the job escaping. The host can still see the job.</span>
                  ) : (
                    <span className="text-amber-400">
                      The host can see this job&apos;s input and output. Do not send private data.
                    </span>
                  )}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="model">Model</Label>
                <Input
                  id="model"
                  aria-invalid={Boolean(errors.model)}
                  {...register("model")}
                />
                {errors.model ? (
                  <p className="text-xs text-danger">{errors.model.message}</p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    <span className="font-mono">echo-sim-v1</span> is the approved
                    simulated model.
                  </p>
                )}
              </div>
            </div>

            {/* --- Workload-specific input editor --- */}
            {workloadType === "text-generation" ? (
              <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
                <div className="space-y-1.5">
                  <Label htmlFor="prompt">Prompt</Label>
                  <textarea
                    id="prompt"
                    rows={4}
                    className={textareaClass}
                    placeholder="Write a haiku about distributed compute…"
                    aria-invalid={Boolean(errors.prompt)}
                    {...register("prompt")}
                  />
                  {errors.prompt ? (
                    <p className="text-xs text-danger">{errors.prompt.message}</p>
                  ) : null}
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="max_tokens">Max tokens</Label>
                  <Input
                    id="max_tokens"
                    type="number"
                    min={1}
                    className="sm:w-32"
                    aria-invalid={Boolean(errors.max_tokens)}
                    {...register("max_tokens")}
                  />
                  {errors.max_tokens ? (
                    <p className="text-xs text-danger">
                      {errors.max_tokens.message}
                    </p>
                  ) : (
                    <p className="text-xs text-muted-foreground">Optional</p>
                  )}
                </div>
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="items_json">Items (JSON)</Label>
                <textarea
                  id="items_json"
                  rows={8}
                  className={`${textareaClass} font-mono`}
                  aria-invalid={Boolean(errors.items_json)}
                  {...register("items_json")}
                />
                {errors.items_json ? (
                  <p className="text-xs text-danger">
                    {errors.items_json.message}
                  </p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    A JSON object with an <span className="font-mono">items</span>{" "}
                    array, e.g.{" "}
                    <span className="font-mono">
                      {'{"items": [{"prompt": "Hello"}]}'}
                    </span>
                    .
                  </p>
                )}
              </div>
            )}

            <div className="space-y-1.5">
              <Label htmlFor="machine_id">Target machine (optional)</Label>
              <Input
                id="machine_id"
                placeholder="Leave blank to let the scheduler pick"
                {...register("machine_id")}
              />
              <p className="text-xs text-muted-foreground">
                Omit to let the scheduler pick the best eligible machine. Paste a
                machine ID from the marketplace to target a specific one.
              </p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="space-y-1.5">
                <Label htmlFor="gpu_count">GPU count</Label>
                <Input
                  id="gpu_count"
                  type="number"
                  min={1}
                  aria-invalid={Boolean(errors.gpu_count)}
                  {...register("gpu_count")}
                />
                {errors.gpu_count ? (
                  <p className="text-xs text-danger">
                    {errors.gpu_count.message}
                  </p>
                ) : null}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="estimated_minutes">Est. minutes</Label>
                <Input
                  id="estimated_minutes"
                  type="number"
                  min={1}
                  aria-invalid={Boolean(errors.estimated_minutes)}
                  {...register("estimated_minutes")}
                />
                {errors.estimated_minutes ? (
                  <p className="text-xs text-danger">
                    {errors.estimated_minutes.message}
                  </p>
                ) : null}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="max_runtime_seconds">Max runtime (s)</Label>
                <Input
                  id="max_runtime_seconds"
                  type="number"
                  min={1}
                  aria-invalid={Boolean(errors.max_runtime_seconds)}
                  {...register("max_runtime_seconds")}
                />
                {errors.max_runtime_seconds ? (
                  <p className="text-xs text-danger">
                    {errors.max_runtime_seconds.message}
                  </p>
                ) : null}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="max_retries">Max retries</Label>
                <Input
                  id="max_retries"
                  type="number"
                  min={0}
                  aria-invalid={Boolean(errors.max_retries)}
                  {...register("max_retries")}
                />
                {errors.max_retries ? (
                  <p className="text-xs text-danger">
                    {errors.max_retries.message}
                  </p>
                ) : null}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="max_cost">Max cost (budget cap)</Label>
              <Input
                id="max_cost"
                inputMode="decimal"
                placeholder="e.g. 5.00"
                className="sm:w-48"
                aria-invalid={Boolean(errors.max_cost)}
                {...register("max_cost")}
              />
              {errors.max_cost ? (
                <p className="text-xs text-danger">{errors.max_cost.message}</p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  The estimate is validated against this cap (over it → rejected).{" "}
                  {CREDITS_LABEL}. Nothing is charged in Phase 4.
                </p>
              )}
            </div>

            <div className="flex justify-end gap-2 border-t border-border pt-4">
              <Button asChild type="button" variant="outline">
                <Link href="/dashboard/jobs">Cancel</Link>
              </Button>
              <Button type="submit" disabled={createMut.isPending || isPreparing}>
                {isPreparing ? (
                  <>
                    <Loader2 className="animate-spin" /> Encrypting payload…
                  </>
                ) : createMut.isPending ? (
                  <>
                    <Loader2 className="animate-spin" /> Submitting…
                  </>
                ) : (
                  "Submit job"
                )}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* ---- Mandatory Customer Key Backup Dialog ---- */}
      <Dialog
        open={backupModalOpen}
        onOpenChange={(open) => {
          if (!open) {
            setBackupModalOpen(false);
            setPendingValues(null);
          }
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <div className="flex items-center gap-2 text-warning">
              <Key className="h-5 w-5" />
              <DialogTitle>Backup Your Private Key (Required)</DialogTitle>
            </div>
            <DialogDescription className="text-left text-sm pt-2">
              You are submitting a private job with end-to-end encryption.
              Your encryption key is generated and stored{" "}
              <strong className="text-foreground">exclusively in this browser</strong> (IndexedDB).
              ComputeCoin servers <strong className="text-foreground">never receive or store</strong> your private key.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3 rounded-md border border-border bg-surface-2/40 p-4 text-xs">
            <div className="flex items-start gap-2 text-warning">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <p className="font-semibold text-foreground">Critical Warning:</p>
                <p className="text-muted-foreground">
                  If you clear your browser cache, use an incognito window, or switch devices without a backup,
                  <strong className="text-foreground"> you will permanently lose access</strong> to your decrypted job results.
                </p>
              </div>
            </div>

            {customerKey ? (
              <div className="pt-1">
                <span className="text-muted-foreground">Public Key:</span>
                <p className="font-mono text-[11px] break-all select-all text-foreground mt-0.5">
                  {customerKey.publicKeyHex}
                </p>
              </div>
            ) : null}
          </div>

          <DialogFooter className="flex-col gap-2 sm:flex-row sm:justify-end">
            <Button
              variant="outline"
              onClick={() => {
                setBackupModalOpen(false);
                setPendingValues(null);
              }}
            >
              Cancel
            </Button>
            <Button
              variant="default"
              onClick={handleDownloadKeyBackup}
              className="gap-2"
            >
              <Download className="h-4 w-4" /> Download Key Backup &amp; Submit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
