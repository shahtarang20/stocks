"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, Info, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { CopyId, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { ExecutionModeBadge, JobStatusBadge } from "@/components/jobs/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError, type JobExecutionMode } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { jobErrorHelp, jobStatusHelp, jobTypeLabel, shortId } from "@/lib/jobs";

/** "lease_expired" -> "Lease expired". Free-text reasons are left as they are. */
function plainReason(reason: string): string {
  return /^[a-z0-9]+(_[a-z0-9]+)*$/.test(reason) ? reason.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : reason;
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 font-medium">{children}</dd>
    </div>
  );
}

function hostKindText(kind: string | null | undefined): string {
  if (kind === "dedicated") return "Dedicated (a server nobody else uses)";
  if (kind === "shared") return "Shared PC (its owner also uses it)";
  return "Not declared";
}

export default function AdminJobDetailPage() {
  const { accessToken } = useAuth();
  const params = useParams<{ jobId: string }>();
  const jobId = params?.jobId ?? "";
  const token = accessToken as string;

  const query = useQuery({
    queryKey: ["admin", "job", jobId],
    enabled: Boolean(accessToken) && Boolean(jobId),
    queryFn: () => api.getAdminJob(token, jobId),
    refetchInterval: 10000,
  });
  const job = query.data;

  const back = (
    <Button asChild variant="outline">
      <Link href="/admin/jobs">
        <ArrowLeft /> All jobs
      </Link>
    </Button>
  );

  if (query.isLoading) {
    return (
      <>
        <ConsolePageHeader title="Job" action={back} />
        <TableSkeleton rows={5} label="Loading the job" />
      </>
    );
  }

  if (query.isError || !job) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    return (
      <>
        <ConsolePageHeader title="Job" action={back} />
        <ErrorState
          title={notFound ? "We could not find that job" : "We could not load this job"}
          message={
            notFound
              ? "There is no job with this ID. Check the link, or go back to the list of jobs."
              : query.error instanceof ApiError
                ? query.error.message
                : undefined
          }
          onRetry={notFound ? undefined : () => query.refetch()}
          retrying={query.isFetching}
        />
      </>
    );
  }

  const events = [...job.events].sort((a, b) => a.seq - b.seq);
  const attempts = [...job.attempts].sort((a, b) => a.attempt_number - b.attempt_number);
  const maxRetries = typeof job.retry_policy?.max_retries === "number" ? job.retry_policy.max_retries : 0;

  return (
    <>
      <ConsolePageHeader
        title={`Job ${shortId(job.job_id)}`}
        description="Everything about this job except what the customer sent and got back. Use it to see why a job is stuck or failed."
        action={
          <div className="flex flex-wrap gap-2">
            {back}
            <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
              <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        <div className="flex items-start gap-2 rounded-md border border-border bg-surface-2 px-3 py-2 text-sm text-muted-foreground" role="note">
          <Info aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            The customer&apos;s input and results are not shown here, because some jobs are private. Opening this page is
            recorded in the audit log.
          </p>
        </div>

        <Card>
          <CardContent className="p-6">
            <div className="flex flex-wrap items-center gap-2">
              <JobStatusBadge status={job.status} />
              <ExecutionModeBadge mode={(job.execution_mode ?? "simulated") as JobExecutionMode} />
              {job.cancel_requested ? <Badge variant="warning">Cancel requested</Badge> : null}
              <CopyId id={job.job_id} label="Copy job id" />
            </div>
            <p className="mt-3 text-sm text-muted-foreground">{jobStatusHelp(job.status)}</p>

            <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
              <Fact label="Customer">
                {job.customer_email ?? "Unknown"}
                <span className="ml-2 inline-block align-middle">
                  <CopyId id={job.customer_id} label="Copy customer id" />
                </span>
              </Fact>
              <Fact label="Type of work">{jobTypeLabel(job.workload_type)}</Fact>
              <Fact label="AI model">
                <span className="font-mono text-xs">{job.model}</span>
              </Fact>
              {job.kit_id ? (
                <Fact label="Kit">
                  <span className="font-mono text-xs">
                    {job.kit_id}
                    {job.kit_version ? ` v${job.kit_version}` : ""}
                  </span>
                </Fact>
              ) : null}
              <Fact label="Privacy level">
                <Badge variant="outline" className="font-mono text-xs">
                  {job.isolation_tier || "container"}
                </Badge>
              </Fact>
              <Fact label="Graphics cards">
                {job.gpu_count} ({(job.required_gpu_runtime ?? "cuda").toUpperCase()})
              </Fact>
              <Fact label="Created">
                <RelativeTime iso={job.created_at} />
              </Fact>
              <Fact label="Started">{job.started_at ? <RelativeTime iso={job.started_at} /> : "Not started"}</Fact>
              <Fact label="Finished">{job.finished_at ? <RelativeTime iso={job.finished_at} /> : "Not finished"}</Fact>
              <Fact label="Most the customer agreed to pay">{job.max_cost ?? "No limit set"}</Fact>
              <Fact label="Final cost (credits)">{job.final_cost ?? "Not charged"}</Fact>
              <Fact label="Time limit">{job.max_runtime_seconds} seconds</Fact>
              <Fact label="Tries">
                {job.attempts_made} {job.attempts_made === 1 ? "retry" : "retries"} used, the customer allows {maxRetries}. If the
                host caused a loss, the platform adds free retries.
              </Fact>
              <Fact label="Computer">
                {job.machine_id ? (
                  <>
                    {job.machine_hostname ?? "Unnamed"}
                    <span className="ml-2 inline-block align-middle">
                      <CopyId id={job.machine_id} label="Copy machine id" />
                    </span>
                    <span className="mt-0.5 block text-xs font-normal text-muted-foreground">{hostKindText(job.host_kind)}</span>
                  </>
                ) : (
                  "Not placed on a computer"
                )}
              </Fact>
              {job.gpu_shared ? (
                <Fact label="Graphics card">
                  Shared with another program
                  {typeof job.gpu_other_processes === "number" ? ` (${job.gpu_other_processes} seen)` : ""}
                  <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
                    Measured by the host&apos;s agent while the job ran. The customer sees a note about it.
                  </span>
                </Fact>
              ) : null}
              {job.verification_valid !== null && job.verification_valid !== undefined ? (
                <Fact label="Result check">
                  {job.verification_valid ? "Passed" : "Failed"}
                  {job.verification_reason ? (
                    <span className="mt-0.5 block text-xs font-normal text-muted-foreground">{plainReason(job.verification_reason)}</span>
                  ) : null}
                </Fact>
              ) : null}
            </dl>

            {job.error_code || job.error_message ? (
              <div className="mt-5 flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
                <AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  <span className="block font-medium">What went wrong</span>
                  <span className="block">{jobErrorHelp(job.error_code)}</span>
                  <span className="mt-1 block text-xs opacity-90">
                    Technical details:{" "}
                    {job.error_code ? <span className="font-mono">{job.error_code}</span> : null}
                    {job.error_code && job.error_message ? " — " : ""}
                    {job.error_message}
                  </span>
                </span>
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            <h2 className="mb-4 text-sm font-semibold">What happened, step by step</h2>
            {events.length === 0 ? (
              <p className="text-sm text-muted-foreground">No steps have been recorded for this job yet.</p>
            ) : (
              <ol>
                {events.map((ev, i) => (
                  <li key={ev.seq} className="flex gap-3 pb-5 last:pb-0">
                    <div className="flex flex-col items-center">
                      <span aria-hidden="true" className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full bg-primary" />
                      {i < events.length - 1 ? <span aria-hidden="true" className="w-px flex-1 bg-border" /> : null}
                    </div>
                    <div className="-mt-0.5 flex-1">
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        {ev.from_state ? (
                          <>
                            <span className="font-mono text-xs text-muted-foreground">{ev.from_state}</span>
                            <span aria-hidden="true" className="text-muted-foreground">→</span>
                          </>
                        ) : null}
                        <span className="font-mono text-xs font-medium">{ev.to_state}</span>
                        <Badge variant="neutral">{ev.actor}</Badge>
                      </div>
                      {ev.reason ? <p className="mt-0.5 text-sm text-muted-foreground">{plainReason(ev.reason)}</p> : null}
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

        <Card>
          <CardContent className="p-6">
            <h2 className="mb-1 text-sm font-semibold">Attempts</h2>
            <p className="mb-4 text-sm text-muted-foreground">
              Each time a computer picked up this job. More than one means the job was moved.
            </p>
            {attempts.length === 0 ? (
              <p className="text-sm text-muted-foreground">No computer has picked this job up yet.</p>
            ) : (
              <ul className="space-y-2">
                {attempts.map((a) => (
                  <li key={a.attempt_number} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm">
                    <span>
                      <span className="font-medium">Attempt {a.attempt_number}</span>
                      {a.machine_id ? (
                        <span className="ml-2 font-mono text-xs text-muted-foreground" title={a.machine_id}>
                          {shortId(a.machine_id)}
                        </span>
                      ) : null}
                    </span>
                    <span className="flex items-center gap-2">
                      <Badge variant={a.status === "running" ? "default" : "neutral"}>{plainReason(a.status)}</Badge>
                      {a.error_code ? <span className="font-mono text-xs text-danger">{a.error_code}</span> : null}
                      <span className="text-xs text-muted-foreground">
                        <RelativeTime iso={a.started_at} />
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
