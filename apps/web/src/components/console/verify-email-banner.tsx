"use client";

import { useState } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

/** Shown to signed-in users whose email is not verified: billing and jobs are blocked until it is. */
export function VerifyEmailBanner() {
  const { user, accessToken } = useAuth();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  if (!user || user.email_verified) return null;

  async function resend() {
    setPending(true);
    setNote(null);
    try {
      await api.resendVerification(accessToken as string);
      setNote("Verification email sent. Check your inbox, then reload this page.");
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Could not send the email. Please try again.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div
      role="status"
      className="mb-6 flex flex-col gap-3 rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm sm:flex-row sm:items-center sm:justify-between"
    >
      <p className="flex items-start gap-2">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
        <span>
          Verify your email ({user.email}) to buy credits and submit jobs.
          {note ? <span className="ml-1 text-muted-foreground">{note}</span> : null}
        </span>
      </p>
      <Button size="sm" variant="outline" onClick={resend} disabled={pending}>
        {pending ? <Loader2 className="animate-spin" /> : null}
        Resend email
      </Button>
    </div>
  );
}
