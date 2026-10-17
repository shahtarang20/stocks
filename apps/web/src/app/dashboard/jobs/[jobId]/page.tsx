"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  Ban,
  Download,
  FileText,
  Info,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Terminal,
} from "lucide-react";
import { Label } from "@/components/ui/label";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  api,
  ApiError,
  CREDITS_LABEL,
  isTerminalJobStatus,
  type Job,
  type JobArtifact,
  type JobEvent,
  type JobLogs,
  type LogLine,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { billingErrorMessage } from "@/lib/billing";
import { formatRelativeTime } from "@/lib/utils";
import {
  isJobActive,
  isSimulated,
  jobStatusLabel,
  jobStatusVariant,
  logLineText,
  shortId,
} from "@/lib/jobs";

function formatBytes(n: number): string {
  if (!n || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function normalizeLogs(logs: JobLogs | undefined): LogLine[] {
  if (!logs) return [];
  if (Array.isArray(logs)) return logs;
  return logs.lines ?? [];
}

export default function JobDetailPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const params = useParams<{ jobId: string }>();
  const jobId = params.jobId;
  const queryClient = useQueryClient();

  const [confirmCancel, setConfirmCancel] = useState(false);
  const [disputeOpen, setDisputeOpen] = useState(false);
  const [disputeReason, setDisputeReason] = useState("");

  const jobQuery = useQuery<Job>({
    queryKey: ["job", jobId],
    enabled: Boolean(accessToken) && Boolean(jobId),
    queryFn: () => api.getJob(token, jobId),
    // Poll the job itself while it is active so status/mode stay fresh.
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s && isJobActive(s) ? 5000 : false;
    },
  });

  const job = jobQuery.data;
  const active = job ? isJobActive(job.status) : false;

  const eventsQuery = useQuery<JobEvent[]>({
    queryKey: ["job-events", jobId],
    enabled: Boolean(accessToken) && Boolean(jobId),
    queryFn: () => api.getJobEvents(token, jobId),
    refetchInterval: active ? 5000 : false,
  });

  const logsQuery = useQuery<JobLogs>({
    queryKey: ["job-logs", jobId],
    enabled: Boolean(accessToken) && Boolean(jobId),
    queryFn: () => api.getJobLogs(token, jobId),
    // Stop polling once the job reaches a terminal state.
    refetchInterval: active ? 4000 : false,
  });

  const artifactsQuery = useQuery<JobArtifact[]>({
    queryKey: ["job-artifacts", jobId],
    enabled: Boolean(accessToken) && Boolean(jobId),
    queryFn: () => api.getJobArtifacts(token, jobId),
    refetchInterval: active ? 6000 : false,
  });

  const cancelMut = useMutation({
    mutationFn: () => api.cancelJob(token, jobId),
    onSuccess: () => {
      setConfirmCancel(false);
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job-events", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const downloadMut = useMutation({
    mutationFn: (artifactId: string) =>
      api.getArtifactDownloadUrl(token, jobId, artifactId),
    onSuccess: (res) => {
      if (res?.url) window.open(res.url, "_blank", "noopener,noreferrer");
    },
  });

  const disputeMut = useMutation({
    mutationFn: (reason: string) =>
      api.openDispute(token, jobId, { reason }),
    onSuccess: () => {
      setDisputeOpen(false);
      setDisputeReason("");
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      queryClient.invalidateQueries({ queryKey: ["disputes"] });
    },
  });

  const simulated = job ? isSimulated(job.execution_mode) : false;
  const canCancel = job ? !isTerminalJobStatus(job.status) : false;
  // A dispute can be opened once a job has settled (completed). Already-disputed
  // jobs can't be disputed again.
  const canDispute = job ? job.status === "completed" : false;

  // ---- Loading / error (primary job fetch) ----
  if (jobQuery.isLoading) {
    return (
      <>
        <ConsolePageHeader title="Job" />
        <Card>
          <CardContent className="space-y-4 p-6">
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-4 w-64" />
            <Skeleton className="h-32 w-full" />
          </CardContent>
        </Card>
      </>
    );
  }

  if (jobQuery.isError || !job) {
    const notFound =
      jobQuery.error instanceof ApiError && jobQuery.error.status === 404;
    return (
      <>
        <ConsolePageHeader title="Job" />
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">
              {notFound ? "Job not found" : "Couldn't load this job"}
            </h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {notFound
                ? "This job doesn't exist, or it belongs to another account."
                : jobQuery.error instanceof ApiError
                  ? jobQuery.error.message
                  : "An unexpected error occurred."}
            </p>
            <Button asChild variant="outline">
              <Link href="/dashboard/jobs">
                <ArrowLeft /> Back to jobs
              </Link>
            </Button>
          </CardContent>
        </Card>
      </>
    );
  }

  const id = job.job_id ?? job.id ?? jobId;
  const events = eventsQuery.data ?? [];
  const sortedEvents = [...events].sort((a, b) => a.seq - b.seq);
  const logLines = normalizeLogs(logsQuery.data).map(logLineText);
  const artifacts = artifactsQuery.data ?? [];
  const match = job.match_explanation;

  return (
    <>
      <ConsolePageHeader
        title={`Job ${shortId(id)}`}
        description="Live job detail from GET /v1/jobs/{id}, its FSM history, logs and artifacts."
        action={
          <div className="flex gap-2">
            <Button asChild variant="outline">
              <Link href="/dashboard/jobs">
                <ArrowLeft /> Back
              </Link>
            </Button>
            {canCancel ? (
              <Button variant="danger" onClick={() => setConfirmCancel(true)}>
                <Ban /> Cancel
              </Button>
            ) : null}
            {canDispute ? (
              <Button variant="outline" onClick={() => setDisputeOpen(true)}>
                <ShieldAlert /> Dispute
              </Button>
            ) : null}
          </div>
        }
      />

      <div className="space-y-6">
        {/* ---- Header / summary ---- */}
        <Card>
          <CardContent className="p-6">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={jobStatusVariant(job.status)}>
                {jobStatusLabel(job.status)}
              </Badge>
              <Badge variant={simulated ? "warning" : "neutral"}>
                {simulated ? "Simulated" : "Hardware"}
              </Badge>
              <span className="font-mono text-xs text-muted-foreground">
                {id}
              </span>
            </div>

            {simulated ? (
              <div className="mt-4 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
                <Info className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                <span className="text-muted-foreground">
                  <span className="font-medium text-foreground">
                    Simulated execution — not real model output.
                  </span>{" "}
                  Outputs and artifacts below are a deterministic transform of the
                  input, not genuine model inference.
                </span>
              </div>
            ) : null}

            <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <dt className="text-muted-foreground">Workload</dt>
                <dd className="mt-0.5 font-medium">{job.workload_type}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Isolation</dt>
                <dd className="mt-0.5">
                  <Badge variant="outline" className="font-mono text-xs uppercase">
                    {job.isolation_tier || "container"}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Model</dt>
                <dd className="mt-0.5 font-mono text-xs">{job.model}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Created</dt>
                <dd className="mt-0.5">{formatRelativeTime(job.created_at)}</dd>
              </div>
              {job.machine_id ? (
                <div>
                  <dt className="text-muted-foreground">Machine</dt>
                  <dd className="mt-0.5 font-mono text-xs">{job.machine_id}</dd>
                </div>
              ) : null}
              {job.max_cost ? (
                <div>
                  <dt className="text-muted-foreground">Budget cap</dt>
                  <dd className="mt-0.5 font-mono">{job.max_cost}</dd>
                </div>
              ) : null}
              <div>
                <dt className="text-muted-foreground">Final cost</dt>
                {job.status === "completed" && job.final_cost != null ? (
                  <dd className="mt-0.5">
                    {/* Real charge from the ledger — shown verbatim. */}
                    <span className="font-mono tabular-nums">
                      {job.final_cost}
                    </span>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {CREDITS_LABEL}
                    </span>
                  </dd>
                ) : job.status === "completed" ? (
                  <dd className="mt-0.5 text-xs text-muted-foreground">
                    settled — awaiting final amount
                  </dd>
                ) : (
                  <dd className="mt-0.5 text-xs text-muted-foreground">
                    not charged
                  </dd>
                )}
              </div>
            </dl>

            {job.error_code || job.error_message ? (
              <div className="mt-4 flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  {job.error_code ? (
                    <span className="font-mono">{job.error_code}</span>
                  ) : null}
                  {job.error_code && job.error_message ? " — " : ""}
                  {job.error_message}
                </span>
              </div>
            ) : null}
          </CardContent>
        </Card>

        {/* ---- Match explanation ---- */}
        {match && (match.machine_id || match.score != null || match.factors) ? (
          <Card>
            <CardContent className="p-6">
              <h2 className="mb-3 text-sm font-semibold">Match explanation</h2>
              <dl className="grid gap-3 text-sm sm:grid-cols-2">
                {match.machine_id ? (
                  <div>
                    <dt className="text-muted-foreground">Chosen machine</dt>
                    <dd className="mt-0.5 font-mono text-xs">
                      {match.machine_id}
                    </dd>
                  </div>
                ) : null}
                {match.score != null ? (
                  <div>
                    <dt className="text-muted-foreground">Score</dt>
                    <dd className="mt-0.5 font-mono">{String(match.score)}</dd>
                  </div>
                ) : null}
              </dl>
              {match.factors && Object.keys(match.factors).length > 0 ? (
                <div className="mt-3">
                  <p className="mb-1 text-xs text-muted-foreground">
                    Score factors
                  </p>
                  <pre className="overflow-x-auto rounded-md border border-border bg-surface-2/40 p-3 text-xs">
                    {JSON.stringify(match.factors, null, 2)}
                  </pre>
                </div>
              ) : null}
            </CardContent>
          </Card>
        ) : null}

        {/* ---- FSM timeline ---- */}
        <Card>
          <CardContent className="p-6">
            <h2 className="mb-4 text-sm font-semibold">FSM timeline</h2>
            {eventsQuery.isLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-10 w-full" />
                ))}
              </div>
            ) : eventsQuery.isError ? (
              <p className="text-sm text-muted-foreground">
                Couldn&apos;t load the event history.
              </p>
            ) : sortedEvents.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No transitions recorded yet.
              </p>
            ) : (
              <ol className="relative space-y-0">
                {sortedEvents.map((ev, i) => (
                  <li key={ev.seq ?? i} className="flex gap-3 pb-5 last:pb-0">
                    <div className="flex flex-col items-center">
                      <span className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full bg-primary" />
                      {i < sortedEvents.length - 1 ? (
                        <span className="w-px flex-1 bg-border" />
                      ) : null}
                    </div>
                    <div className="-mt-0.5 flex-1">
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        {ev.from_state ? (
                          <>
                            <span className="font-mono text-xs text-muted-foreground">
                              {ev.from_state}
                            </span>
                            <span className="text-muted-foreground">→</span>
                          </>
                        ) : null}
                        <span className="font-mono text-xs font-medium">
                          {ev.to_state}
                        </span>
                        {ev.actor ? (
                          <Badge variant="neutral">{ev.actor}</Badge>
                        ) : null}
                      </div>
                      {ev.reason ? (
                        <p className="mt-0.5 text-sm text-muted-foreground">
                          {ev.reason}
                        </p>
                      ) : null}
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {formatRelativeTime(ev.created_at)}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </CardContent>
        </Card>

        {/* ---- Logs ---- */}
        <Card>
          <CardContent className="p-6">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Terminal className="h-4 w-4 text-muted-foreground" /> Logs
              </h2>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => logsQuery.refetch()}
                disabled={logsQuery.isFetching}
              >
                <RefreshCw
                  className={logsQuery.isFetching ? "animate-spin" : undefined}
                />
                Refresh
              </Button>
            </div>
            {logsQuery.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : logsQuery.isError ? (
              <p className="text-sm text-muted-foreground">
                Couldn&apos;t load logs.
              </p>
            ) : logLines.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {active
                  ? "No log lines yet — this job is still working."
                  : "No logs were recorded for this job."}
              </p>
            ) : (
              <pre className="max-h-96 overflow-auto rounded-md border border-border bg-[#0b0f14] p-4 font-mono text-xs leading-relaxed text-green-300">
                {logLines.join("\n")}
              </pre>
            )}
            {active ? (
              <p className="mt-2 text-xs text-muted-foreground">
                Live — logs refresh automatically while the job is active.
              </p>
            ) : null}
          </CardContent>
        </Card>

        {/* ---- Artifacts ---- */}
        <Card>
          <CardContent className="p-6">
            <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold">
              <FileText className="h-4 w-4 text-muted-foreground" /> Artifacts
            </h2>
            {simulated ? (
              <p className="mb-3 text-xs text-warning">
                Simulated execution — artifacts are deterministic output, not real
                model inference.
              </p>
            ) : null}
            {downloadMut.isError ? (
              <div
                role="alert"
                className="mb-3 flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  {downloadMut.error instanceof ApiError
                    ? downloadMut.error.message
                    : "Couldn't get a download link."}
                </span>
              </div>
            ) : null}
            {artifactsQuery.isLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : artifactsQuery.isError ? (
              <p className="text-sm text-muted-foreground">
                Couldn&apos;t load artifacts.
              </p>
            ) : artifacts.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No artifacts{active ? " yet" : ""}.
              </p>
            ) : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {artifacts.map((a) => (
                  <li
                    key={a.artifact_id}
                    className="flex flex-wrap items-center justify-between gap-3 p-3"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Badge variant="neutral">{a.kind}</Badge>
                        <span className="font-mono text-xs text-muted-foreground">
                          {a.content_type}
                        </span>
                      </div>
                      <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground/70">
                        sha256 {a.sha256}
                      </p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {formatBytes(a.size)} · {formatRelativeTime(a.created_at)}
                      </p>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={
                        downloadMut.isPending &&
                        downloadMut.variables === a.artifact_id
                      }
                      onClick={() => downloadMut.mutate(a.artifact_id)}
                    >
                      {downloadMut.isPending &&
                      downloadMut.variables === a.artifact_id ? (
                        <>
                          <Loader2 className="animate-spin" /> Linking…
                        </>
                      ) : (
                        <>
                          <Download /> Download
                        </>
                      )}
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      {/* ---- Cancel confirmation ---- */}
      <Dialog
        open={confirmCancel}
        onOpenChange={(open) => {
          if (!open) {
            setConfirmCancel(false);
            cancelMut.reset();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel this job?</DialogTitle>
            <DialogDescription>
              The scheduler stops the job at the next safe checkpoint and releases
              any reserved capacity. This can&apos;t be undone.
            </DialogDescription>
          </DialogHeader>
          {cancelMut.isError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                {cancelMut.error instanceof ApiError
                  ? cancelMut.error.message
                  : "Couldn't request cancellation."}
              </span>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmCancel(false)}>
              Keep running
            </Button>
            <Button
              variant="danger"
              disabled={cancelMut.isPending}
              onClick={() => cancelMut.mutate()}
            >
              {cancelMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Cancelling…
                </>
              ) : (
                "Cancel job"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---- Open dispute ---- */}
      <Dialog
        open={disputeOpen}
        onOpenChange={(open) => {
          setDisputeOpen(open);
          if (!open) {
            disputeMut.reset();
            setDisputeReason("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Dispute this charge?</DialogTitle>
            <DialogDescription>
              Opening a dispute flags the settled charge for operator review. Tell
              us what looks wrong — this is recorded on the job.
            </DialogDescription>
          </DialogHeader>
          {disputeMut.isError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{billingErrorMessage(disputeMut.error)}</span>
            </div>
          ) : null}
          <div className="space-y-1.5">
            <Label htmlFor="dispute-reason">Reason</Label>
            <textarea
              id="dispute-reason"
              rows={4}
              value={disputeReason}
              onChange={(e) => setDisputeReason(e.target.value)}
              placeholder="e.g. The job failed partway but I was still charged the full amount."
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDisputeOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={disputeMut.isPending || disputeReason.trim().length < 3}
              onClick={() => disputeMut.mutate(disputeReason.trim())}
            >
              {disputeMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Opening…
                </>
              ) : (
                "Open dispute"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
