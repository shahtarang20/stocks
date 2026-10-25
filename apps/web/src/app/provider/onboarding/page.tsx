"use client";

import { useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertCircle,
  AlertTriangle,
  Check,
  Loader2,
  Plus,
  RefreshCw,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { CopyField } from "@/components/provider/copy-field";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  api,
  ApiError,
  API_BASE_URL,
  enrollmentTokenStatus,
  type EnrollmentToken,
  type EnrollmentTokenCreated,
  type EnrollmentTokenStatus,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";

const TOKEN_STATUS_VARIANT: Record<
  EnrollmentTokenStatus,
  "success" | "neutral" | "warning" | "danger"
> = {
  active: "success",
  used: "neutral",
  expired: "warning",
  revoked: "danger",
};

function StepHeader({
  n,
  title,
  done,
}: {
  n: number;
  title: string;
  done?: boolean;
}) {
  return (
    <div className="flex items-center gap-3">
      <span
        className={[
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold",
          done
            ? "bg-success/15 text-success"
            : "bg-primary text-primary-foreground",
        ].join(" ")}
      >
        {done ? <Check className="h-4 w-4" /> : n}
      </span>
      <CardTitle>{title}</CardTitle>
    </div>
  );
}

export default function ProviderOnboardingPage() {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();
  const token = accessToken as string;

  // The raw enrollment secret, held in state only until the provider dismisses
  // it. Honesty constraint: never persisted (no storage), shown once.
  const [newToken, setNewToken] = useState<EnrollmentTokenCreated | null>(null);
  const [label, setLabel] = useState("");
  const [revokeTarget, setRevokeTarget] = useState<EnrollmentToken | null>(null);
  const [registered, setRegistered] = useState(false);

  const tokensQuery = useQuery<EnrollmentToken[]>({
    queryKey: ["provider", "enrollment-tokens"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listEnrollmentTokens(token),
  });

  // If we can list tokens, the user already has the provider role.
  const isProvider = registered || tokensQuery.isSuccess;

  const registerMut = useMutation({
    mutationFn: () => api.providerRegister(token),
    onSuccess: () => {
      setRegistered(true);
      queryClient.invalidateQueries({
        queryKey: ["provider", "enrollment-tokens"],
      });
    },
  });

  const createMut = useMutation({
    mutationFn: () =>
      api.createEnrollmentToken(token, label.trim() ? { label: label.trim() } : {}),
    onSuccess: (created) => {
      setNewToken(created);
      setLabel("");
      queryClient.invalidateQueries({
        queryKey: ["provider", "enrollment-tokens"],
      });
    },
  });

  const revokeMut = useMutation({
    mutationFn: (id: string) => api.revokeEnrollmentToken(token, id),
    onSuccess: () => {
      setRevokeTarget(null);
      queryClient.invalidateQueries({
        queryKey: ["provider", "enrollment-tokens"],
      });
    },
  });

  const tokens = tokensQuery.data ?? [];
  const activeTokenValue = newToken?.enrollment_token ?? "<enr_...>";

  return (
    <>
      <ConsolePageHeader
        title="Provider onboarding"
        description="Become a provider, mint a one-time enrollment token, and connect your machine with the agent. These steps run live against the ComputeCoin API."
      />

      <div className="space-y-6">
        <div
          role="note"
          className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div>
            <p className="font-medium">NVIDIA GPUs only</p>
            <p className="text-muted-foreground">
              To host real GPU jobs your machine needs an NVIDIA GPU, Docker and the NVIDIA
              container toolkit. AMD, Intel and Apple GPUs are not detected, so a machine
              without an NVIDIA GPU shows 0 GPUs and cannot be matched to jobs.
            </p>
          </div>
        </div>

        {/* ---------------------------------------------------------------- */}
        {/* Step 1 — Become a provider                                        */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={1} title="Become a provider" done={isProvider} />
            <CardDescription className="pl-11">
              Grant your account the provider role so you can register hardware.
              This is idempotent — safe to run more than once.
            </CardDescription>
          </CardHeader>
          <CardContent className="pl-11">
            {isProvider ? (
              <p className="flex items-center gap-2 text-sm text-success">
                <Check className="h-4 w-4" /> Your account is a provider.
              </p>
            ) : (
              <div className="space-y-3">
                {registerMut.isError ? (
                  <ErrorNote
                    error={registerMut.error}
                    fallback="Couldn't register as a provider."
                  />
                ) : null}
                <Button
                  onClick={() => registerMut.mutate()}
                  disabled={registerMut.isPending}
                >
                  {registerMut.isPending ? (
                    <>
                      <Loader2 className="animate-spin" /> Registering…
                    </>
                  ) : (
                    "Become a provider"
                  )}
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Step 2 — Generate enrollment token                                */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={2} title="Generate an enrollment token" />
            <CardDescription className="pl-11">
              A single-use, short-lived secret the agent uses once to register a
              machine. The raw value is shown only once.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5 pl-11">
            {/* The one-time secret reveal box. */}
            {newToken ? (
              <div className="space-y-3 rounded-lg border border-warning/50 bg-warning/10 p-4">
                <div className="flex items-start justify-between gap-3">
                  <p className="flex items-start gap-2 text-sm font-medium">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                    <span>
                      <span className="text-warning">Shown once.</span> Copy this
                      token now — you won&apos;t be able to see it again. It
                      expires {formatRelativeTime(newToken.expires_at)} (
                      {new Date(newToken.expires_at).toLocaleString()}).
                    </span>
                  </p>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Dismiss token"
                    onClick={() => setNewToken(null)}
                  >
                    <X />
                  </Button>
                </div>
                <CopyField value={newToken.enrollment_token} />
              </div>
            ) : null}

            {isProvider ? (
              <div className="space-y-3">
                {createMut.isError ? (
                  <ErrorNote
                    error={createMut.error}
                    fallback="Couldn't create an enrollment token."
                  />
                ) : null}
                <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
                  <div className="flex-1 space-y-1.5">
                    <Label htmlFor="token-label">Label (optional)</Label>
                    <Input
                      id="token-label"
                      placeholder="e.g. workstation-rtx4090"
                      value={label}
                      maxLength={120}
                      onChange={(e) => setLabel(e.target.value)}
                    />
                  </div>
                  <Button
                    onClick={() => createMut.mutate()}
                    disabled={createMut.isPending}
                  >
                    {createMut.isPending ? (
                      <>
                        <Loader2 className="animate-spin" /> Generating…
                      </>
                    ) : (
                      <>
                        <Plus /> Generate enrollment token
                      </>
                    )}
                  </Button>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Complete step 1 first to mint enrollment tokens.
              </p>
            )}

            {/* Existing tokens list. */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold">Your enrollment tokens</h3>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => tokensQuery.refetch()}
                  disabled={tokensQuery.isFetching}
                >
                  <RefreshCw
                    className={tokensQuery.isFetching ? "animate-spin" : undefined}
                  />
                  Refresh
                </Button>
              </div>

              {tokensQuery.isLoading ? (
                <div className="space-y-2">
                  {Array.from({ length: 2 }).map((_, i) => (
                    <Skeleton key={i} className="h-10 w-full" />
                  ))}
                </div>
              ) : tokensQuery.isError ? (
                <ErrorNote
                  error={tokensQuery.error}
                  fallback="Couldn't load your enrollment tokens."
                />
              ) : tokens.length === 0 ? (
                <p className="rounded-md border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
                  No enrollment tokens yet. Generate one above to connect a
                  machine.
                </p>
              ) : (
                <div className="rounded-md border border-border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Label</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Expires</TableHead>
                        <TableHead>Created</TableHead>
                        <TableHead className="text-right">Action</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {tokens.map((t) => {
                        const status = enrollmentTokenStatus(t);
                        return (
                          <TableRow key={t.id}>
                            <TableCell className="font-medium">
                              {t.label || (
                                <span className="text-muted-foreground">
                                  (no label)
                                </span>
                              )}
                            </TableCell>
                            <TableCell>
                              <Badge variant={TOKEN_STATUS_VARIANT[status]}>
                                {status}
                              </Badge>
                            </TableCell>
                            <TableCell className="text-sm text-muted-foreground">
                              {formatRelativeTime(t.expires_at)}
                            </TableCell>
                            <TableCell className="text-sm text-muted-foreground">
                              {formatRelativeTime(t.created_at)}
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={status !== "active"}
                                onClick={() => setRevokeTarget(t)}
                              >
                                <Trash2 /> Revoke
                              </Button>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Step 3 — Install & enroll the agent                               */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={3} title="Install & enroll the agent" />
            <CardDescription className="pl-11">
              Run the agent on your machine. It enrolls once with the token
              above, then streams heartbeats to the network.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 pl-11">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Terminal className="h-4 w-4" />
              Server: <code className="font-mono">{API_BASE_URL}</code>
            </div>
            {!newToken ? (
              <p className="text-xs text-muted-foreground">
                Generate a token in step 2 to drop it into the command below.
                Until then <code className="font-mono">&lt;enr_...&gt;</code> is a
                placeholder.
              </p>
            ) : null}
            <CopyField
              label="1. Enroll the machine (one time)"
              value={`computecoin-agent enroll --token ${activeTokenValue} --server ${API_BASE_URL}`}
            />
            <CopyField
              label="2. Start the node (keeps machine online and executes jobs)"
              value={`computecoin-agent start --server ${API_BASE_URL}`}
            />
            <p className="text-xs text-muted-foreground">
              After enrollment, your machine appears under{" "}
              <a href="/provider/hardware" className="text-primary hover:underline">
                Hardware
              </a>{" "}
              with a &quot;Detected&quot; capability badge.
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Revoke confirmation dialog. */}
      <Dialog
        open={Boolean(revokeTarget)}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke enrollment token?</DialogTitle>
            <DialogDescription>
              {revokeTarget?.label
                ? `"${revokeTarget.label}" `
                : "This token "}
              will stop working immediately and can no longer enroll a machine.
              This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {revokeMut.isError ? (
            <ErrorNote
              error={revokeMut.error}
              fallback="Couldn't revoke the token."
            />
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setRevokeTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              disabled={revokeMut.isPending}
              onClick={() =>
                revokeTarget && revokeMut.mutate(revokeTarget.id)
              }
            >
              {revokeMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Revoking…
                </>
              ) : (
                "Revoke token"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ErrorNote({
  error,
  fallback,
}: {
  error: unknown;
  fallback: string;
}) {
  const message = error instanceof ApiError ? error.message : fallback;
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}
