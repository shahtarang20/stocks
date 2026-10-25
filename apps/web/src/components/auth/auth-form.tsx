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
import { api, ApiError, getStoredAccessToken } from "@/lib/api";

const schema = z.object({
  email: z.string().min(1, "Email is required").email("Enter a valid email"),
  password: z.string().min(8, "Password must be at least 8 characters"),
});

type FormValues = z.infer<typeof schema>;

/**
 * Where to go after signing in. The auth guard sends signed-out visitors to
 * /login?next=<page>, but the form ignored it and always landed on /dashboard.
 * Only same-site paths are honoured: "//evil.example" and "https://…" would turn
 * the login page into an open redirect.
 */
function safeNextPath(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
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
    try {
      const email = values.email.trim();
      if (isLogin) {
        await login(email, values.password);
      } else {
        await registerUser(email, values.password);
        if (intent === "host") {
          try {
            await api.providerRegister(getStoredAccessToken() || "cookie-session");
            await refreshUser();
          } catch {
            // Not fatal: the onboarding page offers "Become a provider" as step 1.
          }
          router.push("/provider/onboarding");
          return;
        }
      }
      router.push(safeNextPath(new URL(window.location.href).searchParams.get("next")));
    } catch (err) {
      if (err instanceof ApiError) {
        setServerError(err.message);
      } else {
        setServerError("Something went wrong. Please try again.");
      }
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
            ? "Access the Phase 1 developer console."
            : "Start exploring the developer console. No payment required for the preview."}
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
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{serverError}</span>
            </div>
          ) : null}

          {!isLogin ? (
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">I want to</legend>
              {[
                ["customer", "Use GPUs", "Run jobs on machines from the network."],
                ["host", "Host my GPU", "Share my NVIDIA GPU and earn credits."],
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
                You can do both later from the same account.
              </p>
            </fieldset>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              placeholder="you@dev.io"
              aria-invalid={Boolean(errors.email)}
              {...register("email")}
            />
            {errors.email ? (
              <p className="text-xs text-danger">{errors.email.message}</p>
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
              {...register("password")}
            />
            {errors.password ? (
              <p className="text-xs text-danger">{errors.password.message}</p>
            ) : isLogin ? (
              <p className="text-xs">
                <Link href="/forgot-password" className="text-primary hover:underline">
                  Forgot password?
                </Link>
              </p>
            ) : (
              !isLogin && (
                <p className="text-xs text-muted-foreground">
                  At least 8 characters.
                </p>
              )
            )}
          </div>
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
                Don&apos;t have an account?{" "}
                <Link href="/register" className="text-primary hover:underline">
                  Sign up
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
