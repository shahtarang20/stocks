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
import { useToast } from "@/components/console/toast";

const AVAILABILITY = ["online", "offline", "maintenance"] as const;

const schema = z.object({
  availability: z.enum(AVAILABILITY),
  asking_price_per_hour: z
    .string()
    .trim()
    .refine((v) => v === "" || /^\d+(\.\d{1,4})?$/.test(v), {
      message: "Enter a price like 1.50, or leave it blank.",
    }),
  max_concurrent_jobs: z.coerce
    .number({ invalid_type_error: "Enter a whole number, like 1 or 2." })
    .int("Use a whole number, like 1 or 2.")
    .min(1, "Use 1 or more.")
    .max(999, "That number is too big. Use 999 or less."),
  region: z.string().trim().max(64, "That is too long. Use 64 letters or fewer."),
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
  const toast = useToast();

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
        // "throttled" is agent-set (heartbeat) and not owner-selectable (PATCH rejects it);
        // a throttled machine is online but rate-limited, so seed the form as "online".
        availability: machine.availability === "throttled" ? "online" : machine.availability,
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
      toast.success("Your changes are saved.");
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
          <DialogTitle>Edit price and availability</DialogTitle>
          <DialogDescription>
            {machine?.hostname
              ? `Choose when ${machine.hostname} takes jobs and what it charges.`
              : "Choose when this computer takes jobs and what it charges."}
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
                  : "We could not save your changes. Check your internet connection and try again."}
              </span>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="availability">Can this computer take jobs?</Label>
            <select
              id="availability"
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              {...register("availability")}
            >
              <option value="online">Yes, take jobs (online)</option>
              <option value="offline">No, stay off the network (offline)</option>
              <option value="maintenance">Paused for now (maintenance)</option>
            </select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="asking_price_per_hour">
              Price per hour (credits)
            </Label>
            <Input
              id="asking_price_per_hour"
              inputMode="decimal"
              placeholder="1.50"
              aria-invalid={Boolean(errors.asking_price_per_hour)}
              aria-describedby="asking_price_help"
              {...register("asking_price_per_hour")}
            />
            {errors.asking_price_per_hour ? (
              <p id="asking_price_help" role="alert" className="text-xs text-danger">
                {errors.asking_price_per_hour.message}
              </p>
            ) : (
              <p id="asking_price_help" className="text-xs text-muted-foreground">
                Customers see this price in credits. In this preview credits are test credits, not real money. A computer with no price gets no jobs.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="max_concurrent_jobs">Jobs at the same time</Label>
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
            ) : (
              <p className="text-xs text-muted-foreground">
                How many jobs may run on this computer together. Start with 1.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="region">Where is it? (optional)</Label>
            <Input
              id="region"
              placeholder="For example: us-east"
              aria-invalid={Boolean(errors.region)}
              {...register("region")}
            />
            {errors.region ? (
              <p className="text-xs text-danger">{errors.region.message}</p>
            ) : (
              <p className="text-xs text-muted-foreground">
                A region name such as us-east or eu-west. Customers can filter by it.
              </p>
            )}
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
