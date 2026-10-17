"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, Info, Loader2 } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  api,
  CREDITS_LABEL,
  type CreateJobInput,
  type Job,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { createJobErrorMessage } from "@/lib/jobs";

const selectClass =
  "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

const textareaClass =
  "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background aria-[invalid=true]:border-danger";

const decimal = (v: string) => /^\d+(\.\d{1,2})?$/.test(v.trim());

const schema = z
  .object({
    workload_type: z.enum(["text-generation", "batch-inference"]),
    isolation_tier: z
      .enum(["container", "gvisor", "microvm", "confidential"])
      .default("container"),
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
  });

type FormValues = z.input<typeof schema>;

export default function NewJobPage() {
  const { accessToken } = useAuth();
  const router = useRouter();

  const {
    register,
    handleSubmit,
    watch,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      workload_type: "text-generation",
      isolation_tier: "container",
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

  const createMut = useMutation({
    mutationFn: (input: CreateJobInput) =>
      // Idempotency-Key makes a retried submit return the SAME job (no duplicate).
      api.createJob(accessToken as string, input, crypto.randomUUID()),
    onSuccess: (job: Job) => {
      const id = job.job_id ?? job.id;
      router.push(id ? `/dashboard/jobs/${id}` : "/dashboard/jobs");
    },
  });

  function onSubmit(values: FormValues) {
    let input: Record<string, unknown>;
    if (values.workload_type === "text-generation") {
      input = { prompt: (values.prompt ?? "").trim() };
      const mt = (values.max_tokens ?? "").trim();
      if (mt) input.max_tokens = Number(mt);
    } else {
      // Already validated as {items:[...]} in the schema.
      input = JSON.parse((values.items_json ?? "").trim());
    }

    const payload: CreateJobInput = {
      workload_type: values.workload_type,
      isolation_tier: values.isolation_tier,
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

    createMut.mutate(payload);
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
            {createMut.isError ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{createJobErrorMessage(createMut.error)}</span>
              </div>
            ) : null}

            <div className="grid gap-4 sm:grid-cols-3">
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
                <Label htmlFor="isolation_tier">Isolation Tier</Label>
                <select
                  id="isolation_tier"
                  className={selectClass}
                  {...register("isolation_tier")}
                >
                  <option value="container">Standard Container (Shared Kernel)</option>
                  <option value="gvisor">gVisor Sandbox (runsc)</option>
                  <option value="microvm">microVM (Kata / Firecracker)</option>
                  <option value="confidential">Confidential Computing (AMD SEV-SNP)</option>
                </select>
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
              <Button type="submit" disabled={createMut.isPending}>
                {createMut.isPending ? (
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
    </>
  );
}
