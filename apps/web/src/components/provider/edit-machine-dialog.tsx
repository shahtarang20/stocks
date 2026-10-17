"use client";

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";
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
  ApiError,
  type Machine,
  type UpdateMachineInput,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const AVAILABILITY = ["online", "offline", "maintenance"] as const;

const schema = z.object({
  availability: z.enum(AVAILABILITY),
  asking_price_per_hour: z
    .string()
    .trim()
    .refine((v) => v === "" || /^\d+(\.\d{1,4})?$/.test(v), {
      message: "Enter a decimal like 1.50 (or leave blank)",
    }),
  max_concurrent_jobs: z.coerce
    .number({ invalid_type_error: "Enter a whole number" })
    .int("Must be a whole number")
    .min(1, "Must be at least 1")
    .max(999, "Too large"),
  region: z.string().trim().max(64, "Too long"),
});

type FormValues = z.input<typeof schema>;

export function EditMachineDialog({
  machine,
  open,
  onOpenChange,
}: {
  machine: Machine | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      availability: "offline",
      asking_price_per_hour: "",
      max_concurrent_jobs: 1,
      region: "",
    },
  });

  // Re-seed the form whenever a different machine is opened.
  useEffect(() => {
    if (machine) {
      reset({
        availability: machine.availability,
        asking_price_per_hour: machine.asking_price_per_hour ?? "",
        max_concurrent_jobs: machine.max_concurrent_jobs ?? 1,
        region: machine.region ?? "",
      });
    }
  }, [machine, reset]);

  const updateMut = useMutation({
    mutationFn: (patch: UpdateMachineInput) =>
      api.updateMachine(accessToken as string, machine!.machine_id, patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["provider", "machines"] });
      onOpenChange(false);
    },
  });

  function onSubmit(values: FormValues) {
    const price = String(values.asking_price_per_hour).trim();
    const region = String(values.region).trim();
    const patch: UpdateMachineInput = {
      availability: values.availability,
      max_concurrent_jobs: Number(values.max_concurrent_jobs),
    };
    if (price) patch.asking_price_per_hour = price;
    if (region) patch.region = region;
    updateMut.mutate(patch);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit machine</DialogTitle>
          <DialogDescription>
            {machine?.hostname
              ? `Operational settings for ${machine.hostname}.`
              : "Operational settings."}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
          {updateMut.isError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                {updateMut.error instanceof ApiError
                  ? updateMut.error.message
                  : "Couldn't update the machine."}
              </span>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="availability">Availability</Label>
            <select
              id="availability"
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              {...register("availability")}
            >
              <option value="online">online</option>
              <option value="offline">offline</option>
              <option value="maintenance">maintenance</option>
            </select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="asking_price_per_hour">
              Asking price / hour (USD)
            </Label>
            <Input
              id="asking_price_per_hour"
              inputMode="decimal"
              placeholder="1.50"
              aria-invalid={Boolean(errors.asking_price_per_hour)}
              {...register("asking_price_per_hour")}
            />
            {errors.asking_price_per_hour ? (
              <p className="text-xs text-danger">
                {errors.asking_price_per_hour.message}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Leave blank to keep it unpriced.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="max_concurrent_jobs">Max concurrent jobs</Label>
            <Input
              id="max_concurrent_jobs"
              type="number"
              min={1}
              aria-invalid={Boolean(errors.max_concurrent_jobs)}
              {...register("max_concurrent_jobs")}
            />
            {errors.max_concurrent_jobs ? (
              <p className="text-xs text-danger">
                {errors.max_concurrent_jobs.message}
              </p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="region">Region</Label>
            <Input
              id="region"
              placeholder="us-east"
              aria-invalid={Boolean(errors.region)}
              {...register("region")}
            />
            {errors.region ? (
              <p className="text-xs text-danger">{errors.region.message}</p>
            ) : null}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={isSubmitting || updateMut.isPending}>
              {updateMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Saving…
                </>
              ) : (
                "Save changes"
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
