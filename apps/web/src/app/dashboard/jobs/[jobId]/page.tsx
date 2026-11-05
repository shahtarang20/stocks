"use client";

import { useEffect, useRef, useState } from "react";
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
  CheckCircle2,
  Download,
  FileText,
  Info,
  Key,
  Loader2,
  Lock,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  Unlock,
  XCircle,
} from "lucide-react";
import { Label } from "@/components/ui/label";
import { HelpTip } from "@/components/ui/help-tip";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { LivePanel } from "@/components/jobs/live-panel";
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
  apiAbsoluteUrl,
  ApiError,
  CREDITS_LABEL,
  isTerminalJobStatus,
  type Job,
  type JobArtifact,
  type JobEvent,
  privacyApi,
  type JobLogs,
  type LogLine,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { billingErrorMessage } from "@/lib/billing";
import { capitalize, formatDateTime } from "@/lib/format";
import { CopyButton, RelativeTime } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import { ExecutionModeBadge, JobStatusBadge } from "@/components/jobs/status-badge";
import {
  isJobActive,
  isSimulated,
  jobErrorHelp,
  jobStatusHelp,
  jobTypeLabel,
  logLineText,
  shortId,
} from "@/lib/jobs";
import {
  decryptJobOutput,
  type SealedEnvelope,
  verifyReceiptLocal,
  type ReceiptVerificationResult,
} from "@/lib/ccpp";
import { KitJobPanel } from "@/components/kits/kit-job-panel";
import { getCustomerKey, importCustomerKey, UnbackedKeyError } from "@/lib/customer-keys";

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
  const toast = useToast();

  const [confirmCancel, setConfirmCancel] = useState(false);
  const [disputeOpen, setDisputeOpen] = useState(false);
  const [disputeReason, setDisputeReason] = useState("");

  const [decryptedOutput, setDecryptedOutput] = useState<unknown | null>(null);
  const [decryptionError, setDecryptionError] = useState<string | null>(null);
  const [isDecrypting, setIsDecrypting] = useState(false);
  const [keyImportOpen, setKeyImportOpen] = useState(false);
  const [keyImportInput, setKeyImportInput] = useState("");
  const [keyImportError, setKeyImportError] = useState<string | null>(null);

  const [verifyDialogOpen, setVerifyDialogOpen] = useState(false);
  const [localVerificationResult, setLocalVerificationResult] =
    useState<ReceiptVerificationResult | null>(null);
  const [isVerifyingLocal, setIsVerifyingLocal] = useState(false);

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

  // Polling stops the moment the job turns terminal, which used to freeze the timeline, logs
  // and artifacts one step short (e.g. no "verifying -> completed"). Fetch them once more on that edge.
  const wasActive = useRef(false);
  useEffect(() => {
    if (active) {
      wasActive.current = true;
    } else if (job && wasActive.current) {
      wasActive.current = false;
      for (const key of ["job-events", "job-logs", "job-artifacts"]) {
        queryClient.invalidateQueries({ queryKey: [key, jobId] });
      }
    }
  }, [active, job, jobId, queryClient]);

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
      toast.success("We are cancelling your job. It will stop at the next safe moment.");
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job-events", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const downloadMut = useMutation({
    mutationFn: (artifactId: string) =>
      api.getArtifactDownloadUrl(token, jobId, artifactId),
    onSuccess: (res) => {
      if (res?.url) window.open(apiAbsoluteUrl(res.url), "_blank", "noopener,noreferrer");
    },
  });

  const disputeMut = useMutation({
    mutationFn: (reason: string) =>
      api.openDispute(token, jobId, { reason }),
    onSuccess: () => {
      toast.success("Dispute opened. Our team will review the charge and reply on the Disputes page.");
      setDisputeOpen(false);
      setDisputeReason("");
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      queryClient.invalidateQueries({ queryKey: ["disputes"] });
    },
  });

  const receiptQuery = useQuery({
    queryKey: ["privacy-receipt", jobId],
    queryFn: () => privacyApi.getReceiptByJob(token, jobId),
    // Only private jobs have receipts, and only once one has been issued.
    enabled: Boolean(token && jobId && job?.privacy_receipt_id),
    retry: false,
  });

  const simulated = job ? isSimulated(job.execution_mode) : false;
  const canCancel = job ? !isTerminalJobStatus(job.status) : false;
  // A dispute can be opened once a job has settled (completed). Already-disputed
  // jobs can't be disputed again.
  const canDispute = job ? job.status === "completed" : false;

  useEffect(() => {
    if (!job?.encrypted_output || decryptedOutput) return;
    let cancelled = false;

    async function tryDecrypt() {
      setIsDecrypting(true);
      setDecryptionError(null);
      try {
        const key = await getCustomerKey();
        if (!key) {
          if (!cancelled) {
            setDecryptionError(
              "This browser does not have the private key for this job. Use \"Import key\" and paste the key backup you saved when you started the job."
            );
            setIsDecrypting(false);
          }
          return;
        }
        const effectiveId = job?.job_id ?? job?.id ?? jobId;
        const output = decryptJobOutput(
          job!.encrypted_output as unknown as SealedEnvelope,
          key.privateKeyHex,
          effectiveId
        );
        if (!cancelled) {
          setDecryptedOutput(output);
          setDecryptionError(null);
        }
      } catch (err: any) {
        if (!cancelled) {
          setDecryptionError(
            "This browser's key cannot unlock this result. Open this page in the same browser you used to start the job, or use \"Import key\" with your key backup."
          );
        }
      } finally {
        if (!cancelled) setIsDecrypting(false);
      }
    }

    tryDecrypt();
    return () => {
      cancelled = true;
    };
  }, [job, jobId, decryptedOutput]);

  async function handleImportKey() {
    setKeyImportError(null);
    try {
      if (!keyImportInput.trim()) {
        setKeyImportError("Paste your key backup (the whole file) or your 64-character key first.");
        return;
      }
      let imported;
      try {
        imported = await importCustomerKey(keyImportInput.trim());
      } catch (err) {
        if (!(err instanceof UnbackedKeyError)) throw err;
        if (!window.confirm(`${err.message}\n\nReplace it anyway?`)) {
          setKeyImportError(err.message);
          return;
        }
        imported = await importCustomerKey(keyImportInput.trim(), { replaceUnbackedKey: true });
      }
      setKeyImportOpen(false);
      setKeyImportInput("");

      if (job?.encrypted_output) {
        setIsDecrypting(true);
        try {
          const effectiveId = job.job_id ?? job.id ?? jobId;
          const output = decryptJobOutput(
            job.encrypted_output as unknown as SealedEnvelope,
            imported.privateKeyHex,
            effectiveId
          );
          setDecryptedOutput(output);
          setDecryptionError(null);
        } catch (err: any) {
          setDecryptionError("That key could not unlock this result. Check that you pasted the key for this job. " + (err?.message || ""));
        } finally {
          setIsDecrypting(false);
        }
      }
    } catch (err: any) {
      setKeyImportError("We could not read that key. Paste the whole backup file, or the 64-character key. " + (err?.message || String(err)));
    }
  }

  async function handleVerifyReceiptLocal() {
    if (!receiptQuery.data) return;
    setIsVerifyingLocal(true);
    setVerifyDialogOpen(true);
    try {
      const keys = await privacyApi.getVerifierKeys();
      const res = verifyReceiptLocal(receiptQuery.data as any, keys.verifier_public_key);
      setLocalVerificationResult(res);
    } catch (err: any) {
      setLocalVerificationResult({
        verified: false,
        checks: { error: err?.message || String(err) },
      });
    } finally {
      setIsVerifyingLocal(false);
    }
  }

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
              {notFound ? "We could not find this job" : "We could not load this job"}
            </h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {notFound
                ? "This job does not exist, or it belongs to a different account. Check the link, or go back to your jobs list."
                : jobQuery.error instanceof ApiError
                  ? jobQuery.error.message
                  : "Something went wrong on our side. Try again in a moment."}
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {!notFound ? (
                <Button variant="outline" onClick={() => jobQuery.refetch()} disabled={jobQuery.isFetching}>
                  <RefreshCw className={jobQuery.isFetching ? "animate-spin" : undefined} /> Try again
                </Button>
              ) : null}
              <Button asChild variant="outline">
                <Link href="/dashboard/jobs">
                  <ArrowLeft /> Back to jobs
                </Link>
              </Button>
            </div>
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
        description="See how this job is going, what it cost, what happened step by step, and download your results. This page updates by itself while the job is running."
        action={
          <div className="flex flex-wrap gap-2">
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
        {job.workload_type === "kit" ? <KitJobPanel job={job} token={token} /> : null}

        {/* ---- Header / summary ---- */}
        <Card>
          <CardContent className="p-6">
            <div className="flex flex-wrap items-center gap-2">
              <JobStatusBadge status={job.status} />
              <ExecutionModeBadge mode={job.execution_mode} />
              <span className="inline-flex min-w-0 items-center gap-1">
                <span className="break-all font-mono text-xs text-muted-foreground">{id}</span>
                <CopyButton value={id} label="Copy job id" />
              </span>
            </div>

            {simulated ? (
              <div className="mt-4 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
                <Info className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                <span className="text-muted-foreground">
                  <span className="font-medium text-foreground">
                    Practice mode (simulated) — not real AI output.
                  </span>{" "}
                  The results below are a simple placeholder made from your input. They
                  were not made by a real AI model, so do not rely on them.
                </span>
              </div>
            ) : null}

            <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <dt className="text-muted-foreground">Type of job</dt>
                <dd className="mt-0.5 font-medium">{jobTypeLabel(job.workload_type)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Privacy level</dt>
                <dd className="mt-0.5">
                  <Badge variant="outline" className="font-mono text-xs uppercase">
                    {job.isolation_tier || "container"}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">AI model</dt>
                <dd className="mt-0.5 font-mono text-xs">{job.model}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Created</dt>
                <dd className="mt-0.5">
                  <RelativeTime iso={job.created_at} />
                  <span className="block text-xs text-muted-foreground">{formatDateTime(job.created_at)}</span>
                </dd>
              </div>
              {job.machine_id ? (
                <div>
                  <dt className="text-muted-foreground">Computer that ran it</dt>
                  <dd className="mt-0.5 flex items-center gap-1">
                    <span className="break-all font-mono text-xs">{job.machine_id}</span>
                    <CopyButton value={job.machine_id} label="Copy machine id" />
                  </dd>
                </div>
              ) : null}
              {job.max_cost ? (
                <div>
                  <dt className="text-muted-foreground">Most you agreed to pay</dt>
                  <dd className="mt-0.5 font-mono">{job.max_cost}</dd>
                </div>
              ) : null}
              <div>
                <dt className="text-muted-foreground">Final cost</dt>
                {job.final_cost != null ? (
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
                    The job is done. The final amount will show here soon.
                  </dd>
                ) : (
                  <dd className="mt-0.5 text-xs text-muted-foreground">
                    Not charged yet. You are only charged for jobs that finish.
                  </dd>
                )}
              </div>
            </dl>

            <p className="mt-4 text-sm text-muted-foreground">{jobStatusHelp(job.status)}</p>

            {job.error_code || job.error_message ? (
              <div className="mt-4 flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  <span className="block font-medium">What happened</span>
                  <span className="block">{jobErrorHelp(job.error_code)}</span>
                  <span className="mt-1 block text-xs opacity-90">
                    Technical details for support:{" "}
                    {job.error_code ? (
                      <span className="font-mono">{job.error_code}</span>
                    ) : null}
                    {job.error_code && job.error_message ? " — " : ""}
                    {job.error_message}
                  </span>
                </span>
              </div>
            ) : null}
          </CardContent>
        </Card>

        {/* ---- U19 live data (running jobs only) ---- */}
        <LivePanel jobId={job.job_id ?? job.id ?? jobId} running={job.status === "running"} />

        {/* ---- Match explanation ---- */}
        {match && (match.machine_id || match.score != null || match.factors) ? (
          <Card>
            <CardContent className="p-6">
              <h2 className="mb-3 text-sm font-semibold">
                Why this computer was picked
                <HelpTip term="match score">
                  We pick the free computer that fits your job best, looking at price, speed and how reliable the host has been. A higher score means a better fit.
                </HelpTip>
              </h2>
              <dl className="grid gap-3 text-sm sm:grid-cols-2">
                {match.machine_id ? (
                  <div>
                    <dt className="text-muted-foreground">Chosen computer</dt>
                    <dd className="mt-0.5 font-mono text-xs">
                      {match.machine_id}
                    </dd>
                  </div>
                ) : null}
                {match.score != null ? (
                  <div>
                    <dt className="text-muted-foreground">Fit score</dt>
                    <dd className="mt-0.5 font-mono">{String(match.score)}</dd>
                  </div>
                ) : null}
              </dl>
              {match.factors && Object.keys(match.factors).length > 0 ? (
                <div className="mt-3">
                  <p className="mb-1 text-xs text-muted-foreground">
                    What counted (technical details)
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
            <h2 className="mb-4 text-sm font-semibold">What happened, step by step</h2>
            {eventsQuery.isLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-10 w-full" />
                ))}
              </div>
            ) : eventsQuery.isError ? (
              <p className="text-sm text-muted-foreground">
                We could not load the step-by-step history. Refresh the page to try again.
              </p>
            ) : sortedEvents.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Nothing has happened yet. Steps will appear here as your job moves along.
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
                          {/^[a-z0-9]+(_[a-z0-9]+)+$/.test(ev.reason) ? capitalize(ev.reason.replace(/_/g, " ")) : ev.reason}
                        </p>
                      ) : null}
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        <RelativeTime iso={ev.created_at} />
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
                <Terminal className="h-4 w-4 text-muted-foreground" /> Log (what the computer reported)
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
                We could not load the log. Press Refresh to try again.
              </p>
            ) : logLines.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {active
                  ? "Nothing in the log yet. The job is still working, so check back in a moment."
                  : "This job did not write anything to the log."}
              </p>
            ) : (
              <pre className="max-h-96 overflow-auto rounded-md border border-border bg-[#0b0f14] p-4 font-mono text-xs leading-relaxed text-green-300">
                {logLines.join("\n")}
              </pre>
            )}
            {active ? (
              <p className="mt-2 text-xs text-muted-foreground">
                The log updates by itself while the job is running.
              </p>
            ) : null}
          </CardContent>
        </Card>

        {/* ---- Artifacts ---- */}
        <Card>
          <CardContent className="p-6">
            <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold">
              <FileText className="h-4 w-4 text-muted-foreground" /> Result files
            </h2>
            {simulated ? (
              <p className="mb-3 text-xs text-warning">
                Practice mode (simulated) — these files are a simple placeholder, not real AI output.
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
                    : "We could not make a download link. Press Download to try again."}
                </span>
              </div>
            ) : null}
            {artifactsQuery.isLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : artifactsQuery.isError ? (
              <p className="text-sm text-muted-foreground">
                We could not load the result files. Refresh the page to try again.
              </p>
            ) : artifacts.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {active ? "No result files yet. They appear here when the job finishes." : "This job did not make any result files."}
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
                        {formatBytes(a.size)} · <RelativeTime iso={a.created_at} />
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
                          <Loader2 className="animate-spin" /> Preparing…
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

        {/* ---- Job Output (including in-browser decrypted output) ---- */}
        {job.encrypted_output || job.output ? (
          <Card>
            <CardContent className="p-6">
              <div className="mb-3 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {job.encrypted_output ? (
                    <Lock className="h-4 w-4 text-success" />
                  ) : (
                    <FileText className="h-4 w-4 text-muted-foreground" />
                  )}
                  <h2 className="text-sm font-semibold">
                    {job.encrypted_output ? "Your result (unlocked in this browser)" : "Your result"}
                  </h2>
                </div>
                {job.encrypted_output ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setKeyImportOpen(true)}
                  >
                    <Key className="mr-1.5 h-3.5 w-3.5" /> Import key
                  </Button>
                ) : null}
              </div>

              {job.encrypted_output ? (
                <div>
                  {isDecrypting ? (
                    <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
                      <Loader2 className="h-4 w-4 animate-spin" /> Unlocking your result with the key in this browser…
                    </div>
                  ) : decryptedOutput ? (
                    <div className="space-y-2">
                      <div className="flex items-center gap-1.5 text-xs text-success font-medium">
                        <CheckCircle2 className="h-4 w-4" /> Unlocked on your computer with the private key in this browser.
                      </div>
                      <pre className="max-h-96 overflow-auto rounded-md border border-border bg-[#0b0f14] p-4 font-mono text-xs leading-relaxed text-emerald-300">
                        {typeof decryptedOutput === "string"
                          ? decryptedOutput
                          : JSON.stringify(decryptedOutput, null, 2)}
                      </pre>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      <div
                        role="alert"
                        className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
                      >
                        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                        <div>
                          <p className="font-semibold">We could not unlock your result</p>
                          <p className="text-xs text-danger/90">{decryptionError}</p>
                        </div>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        The locked (encrypted) result we are storing for you:
                      </p>
                      <pre className="max-h-32 overflow-auto rounded-md border border-border bg-surface-2/40 p-3 font-mono text-[11px] text-muted-foreground">
                        {JSON.stringify(job.encrypted_output, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              ) : (
                <pre className="max-h-96 overflow-auto rounded-md border border-border bg-[#0b0f14] p-4 font-mono text-xs leading-relaxed text-blue-300">
                  {typeof job.output === "string" ? job.output : JSON.stringify(job.output, null, 2)}
                </pre>
              )}
            </CardContent>
          </Card>
        ) : null}

        {/* ---- Proof-of-Privacy receipt (private tiers only) ---- */}
        {job.isolation_tier && ["sealed", "sealed_pro", "confidential"].includes(job.isolation_tier) ? (
          <Card>
            <CardContent className="p-6">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <ShieldCheck className="h-5 w-5" />
                  <h2 className="text-sm font-semibold">
                    Privacy receipt
                    <HelpTip term="privacy receipt">
                      A record that shows when your job&apos;s keys were handed out and that the host promised to wipe your data afterwards. It is made only for private jobs.
                    </HelpTip>
                  </h2>
                </div>
                {receiptQuery.data ? (
                  <Badge variant="outline" className="font-mono text-xs">
                    {receiptQuery.data.verified ? "Receipt ready · checked by ComputeCoin" : "Receipt ready · ComputeCoin's check FAILED"}
                  </Badge>
                ) : (
                  <Badge variant="outline" className="font-mono text-xs">
                    No receipt yet
                  </Badge>
                )}
              </div>

              {receiptQuery.isLoading ? (
                <Skeleton className="h-16 w-full" />
              ) : receiptQuery.data ? (
                <div className="space-y-4">
                  <dl className="grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
                    <div>
                      <dt className="text-muted-foreground">Privacy level</dt>
                      <dd className="mt-0.5 font-mono text-xs">{receiptQuery.data.isolation_tier}</dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">Software the host ran</dt>
                      <dd className="mt-0.5 truncate font-mono text-[11px]" title={receiptQuery.data.image_hash}>
                        {receiptQuery.data.image_hash}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">Public log fingerprint</dt>
                      <dd className="mt-0.5 truncate font-mono text-[11px]" title={receiptQuery.data.merkle_root}>
                        {receiptQuery.data.merkle_root}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">Data wiped</dt>
                      <dd className="mt-0.5 font-mono text-xs">
                        {receiptQuery.data.wipe_receipt
                          ? String((receiptQuery.data.wipe_receipt as { wiped_items?: string[] }).wiped_items?.join(", ") ?? "present")
                          : "none"}
                      </dd>
                    </div>
                  </dl>
                  <div className="flex flex-wrap items-center gap-3 pt-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        const blob = new Blob([JSON.stringify(receiptQuery.data, null, 2)], { type: "application/json" });
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement("a");
                        a.href = url;
                        a.download = `privacy-receipt-${id}.json`;
                        a.click();
                        URL.revokeObjectURL(url);
                      }}
                    >
                      <Download className="mr-1.5 h-3.5 w-3.5" /> Download receipt
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleVerifyReceiptLocal}
                      disabled={isVerifyingLocal}
                    >
                      {isVerifyingLocal ? (
                        <>
                          <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Checking…
                        </>
                      ) : (
                        <>
                          <ShieldCheck className="mr-1.5 h-3.5 w-3.5 text-success" /> Check receipt in this browser
                        </>
                      )}
                    </Button>
                    <span className="text-xs text-muted-foreground">
                      For a check that does not rely on us (for technical users):{" "}
                      <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs">
                        python -m computecoin.ccverify privacy-receipt-{id}.json --verifier-key &lt;key from /v1/privacy/verifier-key&gt;
                      </code>
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    The receipt shows which security checks, key hand-overs and wipe promise were logged. It does not
                    prove the host&apos;s hardware is what it says: that hardware check only runs in test setups for now.
                  </p>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">
                  A receipt is made after a private job finishes on a host that passed its security check. Until then there is nothing to check.
                </p>
              )}
            </CardContent>
          </Card>
        ) : null}
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
              We will stop the job at the next safe moment and give back the credits we set aside. You can&apos;t undo this. You are not charged for a cancelled job.
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
                  : "We could not cancel the job. Try again in a moment."}
              </span>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmCancel(false)}>
              Keep it running
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
              A dispute asks our team to look at this charge again. Tell us what looks wrong.
              We save your message with the job and reply on the Disputes page.
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
            <Label htmlFor="dispute-reason">What looks wrong?</Label>
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
                  <Loader2 className="animate-spin" /> Sending…
                </>
              ) : (
                "Open dispute"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---- In-Browser Receipt Verification Modal ---- */}
      <Dialog
        open={verifyDialogOpen}
        onOpenChange={(open) => {
          if (!open) {
            setVerifyDialogOpen(false);
          }
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-success" />
              <DialogTitle>Privacy receipt check</DialogTitle>
            </div>
            <DialogDescription className="text-left text-xs">
              This check runs here in your browser, not on our server. The key it uses
              comes from ComputeCoin. For a fully independent check, use the command-line tool
              with a key you got from somewhere else.
            </DialogDescription>
          </DialogHeader>

          {isVerifyingLocal ? (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" /> Checking signatures and proofs…
            </div>
          ) : localVerificationResult ? (
            <div className="space-y-4">
              <div
                className={`flex items-center justify-between rounded-md p-3 text-sm font-semibold ${
                  localVerificationResult.verified
                    ? "bg-emerald-500/10 text-success border border-emerald-500/30"
                    : "bg-danger/10 text-danger border border-danger/30"
                }`}
              >
                <div className="flex items-center gap-2">
                  {localVerificationResult.verified ? (
                    <CheckCircle2 className="h-5 w-5" />
                  ) : (
                    <XCircle className="h-5 w-5" />
                  )}
                  <span>
                    {localVerificationResult.verified
                      ? "All checks passed"
                      : "Some checks failed"}
                  </span>
                </div>
                <Badge variant="outline" className="font-mono text-xs">
                  {localVerificationResult.isolation_tier}
                </Badge>
              </div>

              <div className="space-y-2 rounded-md border border-border bg-surface-2/40 p-3 text-xs">
                <p className="font-medium text-foreground">Details for each check:</p>
                <ul className="space-y-1.5 font-mono text-[11px]">
                  {Object.entries(localVerificationResult.checks).map(([key, val]) => {
                    const pass = val === true || val === "absent";
                    return (
                      <li
                        key={key}
                        className="flex items-center justify-between border-b border-border/50 pb-1"
                      >
                        <span className="text-muted-foreground">{key}</span>
                        <span
                          className={
                            pass
                              ? "text-success font-bold"
                              : "text-danger font-bold"
                          }
                        >
                          {val === true
                            ? "PASS"
                            : val === "absent"
                              ? "NONE (OPTIONAL)"
                              : val === false
                                ? "FAIL"
                                : String(val)}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>

              <p className="text-xs text-muted-foreground">
                A passing receipt shows that the host received your job&apos;s keys, that every key
                hand-over is in a public log, and that the host signed a promise to wipe your data. It does
                not prove the wipe really happened. The hardware security check is not live yet, so this is
                not proof about the host&apos;s hardware.
              </p>
            </div>
          ) : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => setVerifyDialogOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---- Import Private Key Dialog ---- */}
      <Dialog
        open={keyImportOpen}
        onOpenChange={(open) => {
          if (!open) {
            setKeyImportOpen(false);
            setKeyImportError(null);
            setKeyImportInput("");
          }
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <div className="flex items-center gap-2 text-warning">
              <Key className="h-5 w-5" />
              <DialogTitle>Import your private key</DialogTitle>
            </div>
            <DialogDescription className="text-left text-xs">
              Paste everything in the backup file you saved:{" "}
              <code className="text-foreground">computecoin-private-key-backup.json</code> file,
              or your 64-character private key.
            </DialogDescription>
          </DialogHeader>

          {keyImportError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{keyImportError}</span>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="import-key-input">Your key backup or key</Label>
            <textarea
              id="import-key-input"
              rows={4}
              value={keyImportInput}
              onChange={(e) => setKeyImportInput(e.target.value)}
              placeholder="Paste your key backup or private key here…"
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            />
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setKeyImportOpen(false);
                setKeyImportError(null);
                setKeyImportInput("");
              }}
            >
              Cancel
            </Button>
            <Button variant="default" onClick={handleImportKey}>
              Import key and unlock result
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
