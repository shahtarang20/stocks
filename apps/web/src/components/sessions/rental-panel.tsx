"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, Save } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ApiError, apiAbsoluteUrl, apiErrorCode } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { dataApi } from "@/lib/kits";
import { sessionsApi, type SessionExport, type SessionInfo } from "@/lib/sessions";
import { canRequestExport, exportBusy, exportButtonReason, exportStateText } from "@/lib/rental";
import { formatBytes, formatMbAsGb, formatRelativeTime } from "@/lib/utils";

/** Plain words for what can go wrong when asking the host to save results. */
export function exportErrorText(err: unknown): string {
  const code = apiErrorCode(err);
  if (code === "session_not_exportable" || (err instanceof ApiError && err.status === 409)) {
    return "Results can only be saved while the rental is running.";
  }
  if (code === "export_too_soon" || (err instanceof ApiError && err.status === 429)) {
    return "Please wait a minute before asking again.";
  }
  if (err instanceof ApiError && err.status === 404) return "This rental could not be found.";
  return "We could not ask the host to save your results. Try again in a moment.";
}

/**
 * What a container rental shows on its session page: what it is made of, the files that were brought in, the "save my
 * results now" control, and the files already saved to the customer's storage. Shown only for rentals.
 */
export function RentalPanel({ session }: { session: SessionInfo }) {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const qc = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [linkInfo, setLinkInfo] = useState<Record<string, string>>({});

  const save = useMutation({
    mutationFn: () => sessionsApi.requestExport(session.id),
    onSuccess: () => {
      setError(null);
      setNotice("Asked the host's computer to save your results. This can take a little while for large files.");
      qc.invalidateQueries({ queryKey: ["session", session.id] });
    },
    onError: (err) => {
      setNotice(null);
      setError(exportErrorText(err));
    },
  });

  const download = useMutation({
    mutationFn: (f: SessionExport) => dataApi.downloadLink(token, f.file_id),
    onSuccess: (res, f) => {
      setLinkInfo((m) => ({ ...m, [f.file_id]: res.expires_at ? `Link expires ${formatRelativeTime(res.expires_at)}.` : "This link expires soon." }));
      if (res?.url) window.open(apiAbsoluteUrl(res.url), "_blank", "noopener,noreferrer");
    },
    onError: () => setError("We could not make a download link for that file. Try again in a moment."),
  });

  if (!session.rental) return null;

  const allowed = canRequestExport(session);
  const reason = exportButtonReason(session);
  const inputs = session.inputs ?? [];
  const exports = session.exports ?? [];
  const live = ["pending", "starting", "active"].includes(session.status);

  return (
    <Card className="mb-4" data-testid="rental-panel">
      <CardContent className="space-y-5 p-4 sm:p-6">
        <section aria-labelledby="rental-h">
          <h2 id="rental-h" className="text-lg font-semibold">
            Your container rental
          </h2>
          <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <dt className="text-muted-foreground">Started from</dt>
              <dd className="mt-0.5 font-medium">{session.template ? `The ${session.template} template` : "Your own image"}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Workspace</dt>
              <dd className="mt-0.5 font-medium">{session.workspace_gb ? `${session.workspace_gb} GB` : "No extra workspace"}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Memory and cores</dt>
              <dd className="mt-0.5 font-medium">
                {session.memory_mb ? formatMbAsGb(session.memory_mb) : "Default"}
                {session.cpus ? ` · ${session.cpus} cores` : ""}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Computer</dt>
              <dd className="mt-0.5 font-medium">{session.requested_machine_id ? "The one you chose" : "Picked for you"}</dd>
            </div>
          </dl>
          {live ? (
            <p className="mt-3 rounded-md bg-surface-2 p-3 text-sm text-muted-foreground">
              Your workspace is deleted when the rental ends. Put finished files in the <code className="font-mono text-xs">results</code> folder (
              <code className="font-mono text-xs">/workspace/results</code>) and press Save results now before you stop.
            </p>
          ) : null}
        </section>

        {inputs.length > 0 ? (
          <section aria-labelledby="rental-inputs-h">
            <h3 id="rental-inputs-h" className="text-sm font-semibold">
              Files brought in
            </h3>
            <p className="text-xs text-muted-foreground">Copied into your workspace before the rental started. That time is not charged.</p>
            <ul className="mt-2 space-y-1 text-sm">
              {inputs.map((i) => (
                <li key={i.path} className="flex flex-wrap items-center gap-2">
                  <code className="break-all font-mono text-xs">/workspace/{i.path}</code>
                  <span className="text-xs text-muted-foreground">{formatBytes(i.size_bytes)}</span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        <section aria-labelledby="rental-save-h">
          <h3 id="rental-save-h" className="text-sm font-semibold">
            Saving your results
          </h3>
          <p className="mt-1 text-sm" role="status" aria-live="polite">
            {exportStateText(session.export_state)}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <Button
              variant="outline"
              size="sm"
              disabled={!allowed || save.isPending}
              aria-describedby={reason ? "rental-save-reason" : undefined}
              onClick={() => {
                setNotice(null);
                setError(null);
                save.mutate();
              }}
            >
              <Save aria-hidden="true" className="mr-1 h-3.5 w-3.5" />
              {save.isPending ? "Asking…" : exportBusy(session.export_state) ? "Saving…" : "Save results now"}
            </Button>
            {reason ? (
              <span id="rental-save-reason" className="text-xs text-muted-foreground">
                {reason}
              </span>
            ) : null}
          </div>
          {notice ? (
            <p role="status" className="mt-2 text-sm text-muted-foreground">
              {notice}
            </p>
          ) : null}
          {error ? (
            <p role="alert" className="mt-2 text-sm text-danger">
              {error}
            </p>
          ) : null}
        </section>

        <section aria-labelledby="rental-files-h">
          <h3 id="rental-files-h" className="text-sm font-semibold">
            Saved files
          </h3>
          {exports.length === 0 ? (
            <p className="mt-1 text-sm text-muted-foreground">Nothing saved yet. Files you put in the results folder are saved when you press the button, and again when the rental ends.</p>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">These are also in your stored files.</p>
              <ul className="mt-2 space-y-2">
                {exports.map((f) => (
                  <li key={f.file_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-2 text-sm">
                    <span className="min-w-0">
                      <span className="break-all font-medium">{f.name}</span>{" "}
                      <Badge variant="neutral" className="text-[10px]">
                        {formatBytes(f.size_bytes)}
                      </Badge>
                      {linkInfo[f.file_id] ? <span className="block text-xs text-muted-foreground">{linkInfo[f.file_id]}</span> : null}
                    </span>
                    <Button variant="outline" size="sm" disabled={download.isPending} onClick={() => download.mutate(f)} aria-label={`Download ${f.name}`}>
                      <Download aria-hidden="true" className="mr-1 h-3.5 w-3.5" /> Download
                    </Button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
