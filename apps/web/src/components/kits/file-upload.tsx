"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Pause, Play, X, CheckCircle2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { createUploader, type Uploader, type UploadProgress } from "@/lib/uploader";
import type { DataFile } from "@/lib/kits";
import { formatBytes } from "@/lib/utils";

const PHASE_TEXT: Record<UploadProgress["phase"], string> = {
  hashing: "Checking your file",
  starting: "Starting upload",
  uploading: "Uploading",
  paused: "Paused",
  completing: "Finishing up",
  done: "Uploaded",
  error: "Upload stopped. Press Retry",
  cancelled: "Cancelled",
};

/** One required/optional Kit input: pick a file, upload it resumably, report the data-plane file. */
export function FileUpload({
  label, description, required, token, onUploaded, onCleared, error,
}: {
  label: string;
  description?: string;
  required?: boolean;
  token: string;
  onUploaded: (file: DataFile) => void;
  onCleared: () => void;
  error?: string;
}) {
  const uid = useId();
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [name, setName] = useState<string>("");
  const up = useRef<Uploader | null>(null);

  useEffect(() => () => up.current?.pause(), []);

  function pick(file: File | undefined) {
    if (!file) return;
    const previous = up.current;
    setName(file.name);
    onCleared();
    const u = createUploader({
      token,
      file,
      onProgress: (p) => {
        // A replaced uploader may still be hashing or sending: whatever it reports now belongs to the OLD file
        // and must not overwrite this one's progress or hand the old file back as the input.
        if (up.current !== u) return;
        setProgress(p);
        if (p.phase === "done" && p.file) onUploaded(p.file);
      },
    });
    up.current = u;
    // pause() cannot stop a hash that is still running (the old upload would carry on and finish); cancel() does.
    void previous?.cancel();
    void u.start();
  }

  const phase = progress?.phase;
  const active = phase === "hashing" || phase === "starting" || phase === "uploading" || phase === "completing";
  const pct = Math.round((progress?.fraction ?? 0) * 100);

  return (
    <div className="space-y-2">
      <Label htmlFor={`${uid}-file`}>
        {label}
        {required ? <span className="ml-1 text-danger" aria-hidden="true">*</span> : null}
        {required ? <span className="sr-only"> (required)</span> : null}
      </Label>
      {description ? <p id={`${uid}-d`} className="text-xs text-muted-foreground">{description}</p> : null}
      <input
        id={`${uid}-file`}
        type="file"
        onChange={(e) => pick(e.target.files?.[0])}
        aria-describedby={[description ? `${uid}-d` : "", `${uid}-e`].filter(Boolean).join(" ")}
        aria-invalid={error ? true : undefined}
        className="block w-full text-sm text-foreground file:mr-3 file:rounded-md file:border file:border-border file:bg-surface-2 file:px-3 file:py-2 file:text-sm file:font-medium file:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      />
      {progress ? (
        <div className="rounded-md border border-border bg-surface-2 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="min-w-0 truncate font-medium">{name}</span>
            <span className="text-muted-foreground" role="status" aria-live="polite">
              {phase === "done" ? <CheckCircle2 aria-hidden="true" className="mr-1 inline h-4 w-4 text-success" /> : null}
              {PHASE_TEXT[progress.phase]}
              {progress.deduplicated ? " (we already had this file)" : ""}
              {phase !== "done" && phase !== "error" && phase !== "cancelled" ? ` ${pct}%` : ""}
            </span>
          </div>
          <div
            role="progressbar"
            aria-label={`${label} upload progress`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={phase === "done" ? 100 : pct}
            className="mt-2 h-2 overflow-hidden rounded-full bg-border"
          >
            <div className="h-full bg-primary transition-[width]" style={{ width: `${phase === "done" ? 100 : pct}%` }} />
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            {formatBytes(progress.bytesUploaded)} of {formatBytes(progress.totalBytes)}
            {progress.retries ? ` · tried again ${progress.retries} ${progress.retries === 1 ? "time" : "times"}` : ""}
          </p>
          {phase === "error" ? (
            <p className="mt-1 text-sm text-danger" role="alert">
              We could not upload this file. Check your internet connection, then press Retry.
              {progress.error ? <span className="block text-xs opacity-90">Details: {progress.error}</span> : null}
            </p>
          ) : null}
          <div className="mt-3 flex flex-wrap gap-2">
            {active && phase === "uploading" ? (
              <Button type="button" variant="outline" size="sm" onClick={() => up.current?.pause()}>
                <Pause /> Pause
              </Button>
            ) : null}
            {phase === "paused" || phase === "error" ? (
              <Button type="button" variant="outline" size="sm" onClick={() => void up.current?.resume()}>
                <Play /> {phase === "error" ? "Retry" : "Resume"}
              </Button>
            ) : null}
            {phase !== "done" && phase !== "cancelled" ? (
              <Button type="button" variant="ghost" size="sm" onClick={() => { void up.current?.cancel(); onCleared(); }}>
                <X /> Cancel
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
      <p id={`${uid}-e`} aria-live="polite" className="text-sm text-danger">{error ?? ""}</p>
    </div>
  );
}
