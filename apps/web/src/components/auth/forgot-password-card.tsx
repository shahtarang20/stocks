"use client";

import { useState } from "react";
import Link from "next/link";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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

export function ForgotPasswordCard() {
  const [email, setEmail] = useState("");
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!/^\S+@\S+\.\S+$/.test(email.trim())) {
      setError("That does not look like an email address. Check it and try again.");
      return;
    }
    setPending(true);
    try {
      await api.requestPasswordReset(email.trim());
      setSent(true);
    } catch (err) {
      setError(plainAuthError(err, "forgot"));
    } finally {
      setPending(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">Reset your password</CardTitle>
        <CardDescription>
          Type the email you signed up with. We will send you a link to choose a new password.
        </CardDescription>
      </CardHeader>
      <form onSubmit={onSubmit} noValidate>
        <CardContent className="space-y-4">
          {sent ? (
            <p role="status" className="flex items-start gap-2 text-sm text-success">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              If an account uses that email, a reset link is on its way. Check your inbox and your spam folder.
            </p>
          ) : null}
          {error ? (
            <p role="alert" className="flex items-start gap-2 text-sm text-danger">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              {error}
            </p>
          ) : null}
          <div className="space-y-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
        </CardContent>
        <CardFooter className="flex flex-col gap-4">
          <Button type="submit" className="w-full" disabled={pending}>
            {pending ? (
              <>
                <Loader2 className="animate-spin" /> Sending…
              </>
            ) : (
              "Send reset link"
            )}
          </Button>
          <Link href="/login" className="text-center text-sm text-primary hover:underline">
            Back to log in
          </Link>
        </CardFooter>
      </form>
    </Card>
  );
}
