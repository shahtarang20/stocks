"use client";

import { useRouter, usePathname } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { AlertCircle, Loader2, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

/**
 * Provider pages are for hosts. An account that has not become a host used to see a
 * red "Not registered as a provider" error on every provider page. This shows one
 * clear prompt instead, with the button that fixes it. Onboarding is exempt: it is the
 * page where the account becomes a host.
 */
export function ProviderGate({ children }: { children: React.ReactNode }) {
  const { user, accessToken, refreshUser } = useAuth();
  const pathname = usePathname();
  const router = useRouter();

  const becomeHost = useMutation({
    mutationFn: () => api.providerRegister(accessToken as string),
    onSuccess: async () => {
      await refreshUser();
      router.push("/provider/onboarding");
    },
  });

  const isHost = !user || user.roles.includes("provider");
  if (isHost || pathname.startsWith("/provider/onboarding")) return <>{children}</>;

  return (
    <Card className="mx-auto mt-8 max-w-xl">
      <CardContent className="space-y-4 p-8 text-center">
        <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
          <Server className="h-6 w-6" />
        </span>
        <h2 className="text-lg font-semibold">You are not a host yet</h2>
        <p className="text-sm text-muted-foreground">
          This area is for people who share their GPU on the network. Your account is a
          customer account. You can add hosting to it at any time, and keep using it as a
          customer too.
        </p>
        {becomeHost.isError ? (
          <p role="alert" className="flex items-start justify-center gap-2 text-sm text-danger">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            {becomeHost.error instanceof ApiError
              ? becomeHost.error.message
              : "Could not set up hosting. Please try again."}
          </p>
        ) : null}
        <Button onClick={() => becomeHost.mutate()} disabled={becomeHost.isPending}>
          {becomeHost.isPending ? <Loader2 className="animate-spin" /> : null}
          Become a host
        </Button>
      </CardContent>
    </Card>
  );
}
