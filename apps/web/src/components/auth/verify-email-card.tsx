"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "@/lib/api";
import { plainAuthError } from "./auth-errors";

type State = "verifying" | "done" | "error";

export function VerifyEmailCard() {
  const token = useSearchParams().get("token");
  const [state, setState] = useState<State>(token ? "verifying" : "error");
  const [message, setMessage] = useState(
    token ? "" : "This link is not complete. Open the link from your email again, or log in and ask for a new email.",
  );
  // Tokens are single-use: React strict mode runs effects twice in dev, and a
  // second call would turn a success into "invalid or expired".
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    api
      .verifyEmail(token)
      .then(() => setState("done"))
      .catch((err) => {
        setState("error");
        setMessage(
          plainAuthError(err, "verify"),
        );
      });
  }, [token]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">Verify your email</CardTitle>
        <CardDescription>We are checking your email address.</CardDescription>
      </CardHeader>
      <CardContent>
        {state === "verifying" ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Verifying…
          </p>
        ) : null}
        {state === "done" ? (
          <p role="status" className="flex items-start gap-2 text-sm text-success">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
            Your email is verified. You can now add credits and run jobs.
          </p>
        ) : null}
        {state === "error" ? (
          <p role="alert" className="flex items-start gap-2 text-sm text-danger">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            {message}
          </p>
        ) : null}
      </CardContent>
      <CardFooter>
        <Button asChild className="w-full">
          <Link href="/login">Continue to log in</Link>
        </Button>
      </CardFooter>
    </Card>
  );
}
