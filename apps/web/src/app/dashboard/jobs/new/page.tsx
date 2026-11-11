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
import { HelpTip } from "@/components/ui/help-tip";
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
    model: z.string().trim().min(1, "Enter the name of an AI model. If you are not sure, use echo-sim-v1."),
    prompt: z.string().optional(),
    max_tokens: z
      .string()
      .optional()
      .refine((v) => !v || /^\d+$/.test(v.trim()), {
        message: "Use a whole number, like 64.",
      }),
    items_json: z.string().optional(),
    machine_id: z.string().optional(),
    gpu_count: z.coerce
      .number({ invalid_type_error: "Enter a whole number, like 1." })
      .int("Use a whole number, like 1.")
      .min(1, "Enter 1 or more."),
    estimated_minutes: z.coerce
      .number({ invalid_type_error: "Enter a whole number, like 5." })
      .int("Use a whole number, like 5.")
      .min(1, "Enter 1 minute or more."),
    max_cost: z
      .string()
      .trim()
      .refine(decimal, { message: "Enter an amount like 5.00." }),
    max_runtime_seconds: z.coerce
      .number({ invalid_type_error: "Enter a whole number of seconds, like 300." })
      .int("Use a whole number of seconds, like 300.")
      .min(1, "Enter 1 second or more."),
    max_retries: z.coerce
      .number({ invalid_type_error: "Enter a whole number, like 1." })
      .int("Use a whole number, like 1.")
      .min(0, "Use 0 or more.")
      .max(10, "Use 10 or less."),
    require_dedicated_host: z.boolean().optional(),
  })
  .superRefine((val, ctx) => {
    if (val.workload_type === "text-generation") {
      if (!val.prompt || val.prompt.trim().length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["prompt"],
          message: "Write a prompt first. It is the instruction you want the AI to follow.",
        });
      }
    } else {
      // batch-inference — require a JSON object with a non-empty items array.
      const raw = (val.items_json ?? "").trim();
      if (!raw) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["items_json"],
          message: "Add your list of prompts. See the example below the box.",
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
          message: "We could not read this list. Check for a missing comma, quote or bracket.",
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
          message: 'The list must look like {"items": [ ... ]} and have at least one item.',
        });
      }
    }
    if (val.required_gpu_runtime === "rocm" && !["container", "sealed", "sealed_pro"].includes(val.isolation_tier)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["isolation_tier"],
        message: "AMD graphics cards can run Standard, Private or Extra private jobs only. Pick one of those, or switch the graphics card brand to NVIDIA.",
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
      require_dedicated_host: false,
    },
  });

  const workloadType = watch("workload_type");
  const isolationTier = watch("isolation_tier");
  const requiredGpuRuntime = watch("required_gpu_runtime");
  const isPrivateTier = Boolean(
    isolationTier && ["sealed", "sealed_pro", "confidential"].includes(isolationTier)
  );

  useEffect(() => {
    if (requiredGpuRuntime === "rocm" && !["container", "sealed", "sealed_pro"].includes(isolationTier ?? "")) {
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
    if (values.require_dedicated_host) {
      payload.data_policy = { ...(payload.data_policy || {}), require_dedicated_host: true };
    }

    if (
      values.isolation_tier &&
      ["sealed", "sealed_pro", "confidential"].includes(values.isolation_tier)
    ) {
      if (!key) {
        setPrepError("We could not find your private key in this browser. Reload the page and try again.");
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
        setPrepError("We could not lock (encrypt) your job input, so nothing was sent. Try again, or pick a different privacy level. Details: " + (err?.message || String(err)));
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
      setPrepError("We could not save your key backup, so the job was not sent. Try the download again. Details: " + (err?.message || String(err)));
    }
  }

  return (
    <>
      <ConsolePageHeader
        title="Start a new job"
        description="Tell us what to run and the most you are willing to pay. We check your job, find a host's computer that fits, set your credits aside and start it."
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
            Right now, jobs usually run in practice mode (also called simulated).
          </span>{" "}
          In practice mode the result is a simple placeholder made from your input. It is{" "}
          <span className="font-medium">not real AI output</span>, so do not rely on it. You only use
          test credits, never real money.
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
                <Label htmlFor="workload_type">Type of job</Label>
                <select
                  id="workload_type"
                  className={selectClass}
                  {...register("workload_type")}
                >
                  <option value="text-generation">Answer one prompt (text-generation)</option>
                  <option value="batch-inference">Answer a list of prompts (batch-inference)</option>
                </select>
                <p className="mt-1 text-xs text-muted-foreground">
                  Choose &quot;one prompt&quot; to ask one question. Choose &quot;a list&quot; to send many at once.
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="required_gpu_runtime">Graphics card brand</Label>
                <select
                  id="required_gpu_runtime"
                  className={selectClass}
                  {...register("required_gpu_runtime")}
                >
                  <option value="cuda">NVIDIA (CUDA)</option>
                  <option value="rocm">AMD (ROCm)</option>
                  <option value="any">Either brand</option>
                </select>
                <p className="mt-1 text-xs text-muted-foreground">
                  {requiredGpuRuntime === "rocm"
                    ? "AMD graphics cards can run Standard, Private and Extra private jobs only."
                    : "The brand of graphics card (GPU) your job needs. If you are not sure, keep NVIDIA."}
                </p>
              </div>
              <div className="space-y-1.5">
                <div>
                  <Label htmlFor="isolation_tier">Privacy level</Label>
                  <HelpTip term="privacy level">
                    How well the host&apos;s computer is walled off from your job. Standard is the plain choice.
                    The private levels lock your input so it is harder for the host to read, but they need
                    special security hardware (such as a TPM chip) in the host&apos;s computer. Fewer computers
                    have it, so these jobs can wait longer.
                  </HelpTip>
                </div>
                <select
                  id="isolation_tier"
                  className={selectClass}
                  {...register("isolation_tier")}
                >
                  <option value="container">Standard (locked-down container)</option>
                  <option value="gvisor" disabled={requiredGpuRuntime === "rocm"}>
                    Stronger wall (gVisor){requiredGpuRuntime === "rocm" ? " (not for AMD)" : ""}
                  </option>
                  <option value="microvm" disabled={requiredGpuRuntime === "rocm"}>
                    Stronger wall (microVM){requiredGpuRuntime === "rocm" ? " (not for AMD)" : ""}
                  </option>
                  <option value="sealed">
                    Private (sealed){requiredGpuRuntime === "rocm" ? " (host needs the sealed setup)" : ""}
                  </option>
                  <option value="sealed_pro">
                    Extra private (sealed pro){requiredGpuRuntime === "rocm" ? " (host needs the sealed setup)" : ""}
                  </option>
                  <option value="confidential" disabled={requiredGpuRuntime === "rocm"}>
                    Confidential{requiredGpuRuntime === "rocm" ? " (not for AMD)" : ""}
                  </option>
                </select>
                <p className="mt-1 text-xs text-muted-foreground">
                  {isPrivateTier ? (
                    <span className="text-success">
                      Your input is locked (encrypted) in this browser. Only your browser can unlock the
                      result, and ComputeCoin&apos;s servers never see either. The host&apos;s computer
                      must unlock the input in memory to work on it. This level needs a host with special
                      security hardware. The proof that the hardware is real has not been tested on
                      real machines yet, so today you also trust the host to run the published private
                      setup.
                      {GUARDIAN_PIN ? null : (
                        <>
                          {" "}This version of the site does not double-check the safety keys, so you are
                          trusting ComputeCoin to hand out the right ones.
                        </>
                      )}
                    </span>
                  ) : isolationTier === "gvisor" || isolationTier === "microvm" ? (
                    <span>A stronger wall around your job on the host&apos;s computer. The host can still see what you send.</span>
                  ) : (
                    <span className="text-warning">
                      The host can see what you send and what comes back. Do not send private or secret data.
                    </span>
                  )}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="model">AI model</Label>
                <Input
                  id="model"
                  aria-invalid={Boolean(errors.model)}
                  {...register("model")}
                />
                {errors.model ? (
                  <p className="text-xs text-danger">{errors.model.message}</p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    <span className="font-mono">echo-sim-v1</span> is the practice
                    model. It does not run real AI. Keep it for now.
                  </p>
                )}
              </div>
            </div>

            {/* --- Workload-specific input editor --- */}
            {workloadType === "text-generation" ? (
              <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
                <div className="space-y-1.5">
                  <Label htmlFor="prompt">Your prompt</Label>
                  <textarea
                    id="prompt"
                    rows={4}
                    className={textareaClass}
                    placeholder="For example: Write a short poem about the sea."
                    aria-invalid={Boolean(errors.prompt)}
                    {...register("prompt")}
                  />
                  {errors.prompt ? (
                    <p className="text-xs text-danger">{errors.prompt.message}</p>
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      The question or instruction you want the AI to answer.
                    </p>
                  )}
                </div>
                <div className="space-y-1.5">
                  <div>
                    <Label htmlFor="max_tokens">Longest answer</Label>
                    <HelpTip term="token">
                      A token is a small piece of a word. Longer answers use more tokens, so they take longer.
                    </HelpTip>
                  </div>
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
                    <p className="text-xs text-muted-foreground">Optional. Counted in tokens.</p>
                  )}
                </div>
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="items_json">Your list of prompts</Label>
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
                    Write your list in the format below (this format is called JSON). Put one
                    prompt inside each pair of curly brackets, for example{" "}
                    <span className="font-mono">
                      {'{"items": [{"prompt": "Hello"}]}'}
                    </span>
                    .
                  </p>
                )}
              </div>
            )}

            <div className="space-y-1.5">
              <Label htmlFor="machine_id">Choose a specific computer (optional)</Label>
              <Input
                id="machine_id"
                placeholder="Leave empty and we will pick one for you"
                {...register("machine_id")}
              />
              <p className="text-xs text-muted-foreground">
                Most people leave this empty, and we pick the best computer that is free.
                To use a particular computer, paste its ID from the marketplace.
              </p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="space-y-1.5">
                <Label htmlFor="gpu_count">Number of graphics cards (GPUs)</Label>
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
                ) : (
                  <p className="text-xs text-muted-foreground">How many your job needs. Most jobs need 1.</p>
                )}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="estimated_minutes">Expected run time (minutes)</Label>
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
                ) : (
                  <p className="text-xs text-muted-foreground">Your best guess. We use it to work out the price.</p>
                )}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="max_runtime_seconds">Time limit (seconds)</Label>
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
                ) : (
                  <p className="text-xs text-muted-foreground">The job is stopped after this long. 300 seconds is 5 minutes.</p>
                )}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="max_retries">Tries again if a computer fails</Label>
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
                ) : (
                  <p className="text-xs text-muted-foreground">How many times we move your job to another computer. 0 means never. If a computer fails or its owner stops your job, we try one more computer for free, and you are never charged for the stopped attempt.</p>
                )}
              </div>
            </div>

            <div className="flex items-start gap-2">
              <input
                id="require_dedicated_host"
                type="checkbox"
                className="mt-1 h-4 w-4"
                {...register("require_dedicated_host")}
              />
              <div className="space-y-0.5">
                <Label htmlFor="require_dedicated_host">Only use dedicated computers (recommended for important work)</Label>
                <p className="text-xs text-muted-foreground">
                  A dedicated computer is a server that only runs ComputeCoin jobs. A shared PC (a laptop or gaming computer) is
                  also used by its owner, so your job may run slower or be moved. Fewer computers qualify, so your job may wait longer.
                  The computer's owner declares this.
                </p>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="max_cost">Most you will pay (credits)</Label>
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
                  We will not start the job if the price is higher than this. Amounts are in credits.{" "}
                  {CREDITS_LABEL}.
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
                    <Loader2 className="animate-spin" /> Locking your input…
                  </>
                ) : createMut.isPending ? (
                  <>
                    <Loader2 className="animate-spin" /> Sending your job…
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
              <DialogTitle>Save a backup of your private key</DialogTitle>
            </div>
            <DialogDescription className="text-left text-sm pt-2">
              You picked a private job, so your input is locked (encrypted). The key that unlocks your
              results is kept{" "}
              <strong className="text-foreground">only in this browser</strong>.
              ComputeCoin <strong className="text-foreground">never receives or stores</strong> your private key.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3 rounded-md border border-border bg-surface-2/40 p-4 text-xs">
            <div className="flex items-start gap-2 text-warning">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <p className="font-semibold text-foreground">Please read this:</p>
                <p className="text-muted-foreground">
                  If you clear your browser data, use a private window, or switch to another device without a backup,
                  <strong className="text-foreground"> you will lose your results for good</strong>. We cannot recover them for you.
                </p>
              </div>
            </div>

            {customerKey ? (
              <div className="pt-1">
                <span className="text-muted-foreground">Public key (safe to share):</span>
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
              <Download className="h-4 w-4" /> Download key backup and send job
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
