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
import { ApiError } from "@/lib/api";

const schema = z.object({
  email: z.string().min(1, "Email is required").email("Enter a valid email"),
  password: z.string().min(8, "Password must be at least 8 characters"),
});

type FormValues = z.infer<typeof schema>;

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const { login, register: registerUser } = useAuth();
  const [serverError, setServerError] = useState<string | null>(null);

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
      }
      router.push("/dashboard");
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
          {serverError ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{serverError}</span>
            </div>
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
