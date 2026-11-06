"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Loader2, AlertCircle } from "lucide-react";
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
import { useAuth } from "@/lib/auth-context";
import { api, getStoredAccessToken } from "@/lib/api";
import { plainAuthError } from "./auth-errors";

// U16: must match the API's AUP_CURRENT_VERSION. The API only requires acceptance when AUP_REQUIRED is on.
const AUP_VERSION = process.env.NEXT_PUBLIC_AUP_VERSION ?? "draft-0";

const schema = z.object({
  email: z.string().min(1, "Enter your email address.").email("That does not look like an email address. Check it and try again."),
  password: z.string().min(8, "That password is too short. Use at least 8 characters."),
});

type FormValues = z.infer<typeof schema>;

/**
 * Where to go after signing in. The auth guard sends signed-out visitors to
 * /login?next=<page>, but the form ignored it and always landed on /dashboard.
 * Only same-site paths are honoured: "//evil.example" and "https://…" would turn
 * the login page into an open redirect.
 */
export function safeNextPath(next: string | null): string {
  // Browsers drop tab/newline/control characters when parsing a URL, so "/\t/evil.example" becomes "//evil.example".
  // eslint-disable-next-line no-control-regex
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\") || /[\u0000-\u001f\u007f]/.test(next)) {
    return "/dashboard";
  }
  return next;
}

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const { login, register: registerUser, refreshUser } = useAuth();
  // Who is signing up. Everyone gets a customer account; choosing "host" also makes
  // them a provider straight away and sends them to onboarding.
  const [intent, setIntent] = useState<"customer" | "host">("customer");
  const [serverError, setServerError] = useState<string | null>(null);
  const [changedNotice, setChangedNotice] = useState(false);
  const [acceptAup, setAcceptAup] = useState(false);
  const [aupError, setAupError] = useState(false);

  const {
    register,
    handleSubmit,
    setValue,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: "", password: "" },
  });

  const isLogin = mode === "login";

  // Prefill the email from ?email= (e.g. an invitation or verification link).
  //
  // The password is deliberately NOT read from the query string, and if one is
  // present it is stripped from the URL rather than used. A password in a URL
  // leaks into browser history, server and proxy access logs, and the Referer
  // header sent to any third-party resource the page loads — so accepting one
  // here would turn a stray link into a durable credential disclosure.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (url.searchParams.get("changed") === "1") setChangedNotice(true);
    const emailParam = url.searchParams.get("email");
    if (emailParam) {
      setValue("email", emailParam, { shouldValidate: true });
    }
    if (url.searchParams.has("password")) {
      url.searchParams.delete("password");
      window.history.replaceState(null, "", url.toString());
    }
  }, [setValue]);

  async function onSubmit(values: FormValues) {
    setServerError(null);
    if (!isLogin && !acceptAup) {
      setAupError(true);
      return;
    }
    setAupError(false);
    try {
      const email = values.email.trim();
      if (isLogin) {
        await login(email, values.password);
      } else {
        await registerUser(email, values.password, { accept_aup: true, aup_version: AUP_VERSION });
        if (intent === "host") {
          try {
            await api.providerRegister(getStoredAccessToken() || "cookie-session");
            await refreshUser();
          } catch {
            // Not fatal: the onboarding page offers "Become a host" as a step.
          }
          router.push("/provider/onboarding");
          return;
        }
      }
      router.push(safeNextPath(new URL(window.location.href).searchParams.get("next")));
    } catch (err) {
      setServerError(plainAuthError(err, isLogin ? "login" : "register"));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">
          {isLogin ? "Log in to ComputeCoin" : "Create your account"}
        </CardTitle>
        <CardDescription>
          {isLogin
            ? "Welcome back. Log in to run jobs or manage your computers."
            : "It is free to join the preview. Credits in this preview are test credits, not real money."}
        </CardDescription>
      </CardHeader>
      <form
        method="POST"
        action="#"
        onSubmit={(e) => {
          e.preventDefault();
          handleSubmit(onSubmit)(e);
        }}
        noValidate
      >
        <CardContent className="space-y-4">
          {isLogin && changedNotice ? (
            <div
              role="status"
              className="rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm text-success"
            >
              Password changed. Sign in with your new password.
            </div>
          ) : null}
          {serverError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{serverError}</span>
            </div>
          ) : null}

          {!isLogin ? (
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">I want to</legend>
              {[
                ["customer", "Rent a GPU", "Run your work on a graphics card (GPU) from someone else's computer."],
                ["host", "Share my computer", "Let others use my computer's GPU and earn credits. Today this needs a Linux computer with an NVIDIA or AMD graphics card. Windows PCs with an NVIDIA card are planned, for ordinary jobs only, and are not available yet. See the setup steps after you sign up."],
              ].map(([value, title, hint]) => (
                <label
                  key={value}
                  htmlFor={`intent-${value}`}
                  className={`flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 text-sm ${
                    intent === value ? "border-primary bg-primary/5" : "border-border"
                  }`}
                >
                  <input
                    id={`intent-${value}`}
                    type="radio"
                    name="intent"
                    value={value}
                    checked={intent === value}
                    onChange={() => setIntent(value as "customer" | "host")}
                    className="mt-1"
                  />
                  <span>
                    <span className="block font-medium">{title}</span>
                    <span className="text-xs text-muted-foreground">{hint}</span>
                  </span>
                </label>
              ))}
              <p className="text-xs text-muted-foreground">
                You can do both later with the same account.
              </p>
            </fieldset>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              placeholder="you@example.com"
              aria-invalid={Boolean(errors.email)}
              aria-describedby={errors.email ? "email-error" : undefined}
              {...register("email")}
            />
            {errors.email ? (
              <p id="email-error" role="alert" className="text-xs text-danger">{errors.email.message}</p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              autoComplete={isLogin ? "current-password" : "new-password"}
              placeholder="••••••••"
              aria-invalid={Boolean(errors.password)}
              aria-describedby={errors.password ? "password-error" : undefined}
              {...register("password")}
            />
            {errors.password ? (
              <p id="password-error" role="alert" className="text-xs text-danger">{errors.password.message}</p>
            ) : isLogin ? (
              <p className="text-xs">
                <Link href="/forgot-password" className="text-primary hover:underline">
                  Forgot password?
                </Link>
              </p>
            ) : (
              !isLogin && (
                <p className="text-xs text-muted-foreground">
                  Use at least 8 characters.
                </p>
              )
            )}
          </div>

          {!isLogin ? (
            <div className="space-y-1.5">
              <label htmlFor="accept-aup" className="flex cursor-pointer items-start gap-2 text-sm">
                <input
                  id="accept-aup"
                  type="checkbox"
                  checked={acceptAup}
                  onChange={(e) => {
                    setAcceptAup(e.target.checked);
                    if (e.target.checked) setAupError(false);
                  }}
                  aria-invalid={aupError}
                  aria-describedby={aupError ? "aup-error" : undefined}
                  className="mt-1"
                />
                <span>
                  I have read and accept the{" "}
                  <Link href="/legal/aup" target="_blank" className="text-primary hover:underline">
                    Acceptable Use Policy
                  </Link>
                  .
                </span>
              </label>
              {aupError ? (
                <p id="aup-error" role="alert" className="text-xs text-danger">
                  You need to accept the Acceptable Use Policy to create an account. Tick the box above.
                </p>
              ) : null}
            </div>
          ) : null}
        </CardContent>
        <CardFooter className="flex flex-col gap-4">
          <Button type="submit" className="w-full" disabled={isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className="animate-spin" />
                {isLogin ? "Logging in…" : "Creating account…"}
              </>
            ) : isLogin ? (
              "Log in"
            ) : (
              "Create account"
            )}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            {isLogin ? (
              <>
                New here?{" "}
                <Link href="/register" className="text-primary hover:underline">
                  Create an account
                </Link>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <Link href="/login" className="text-primary hover:underline">
                  Log in
                </Link>
              </>
            )}
          </p>
        </CardFooter>
      </form>
    </Card>
  );
}
