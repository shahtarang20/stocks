"use client";

/**
 * Admin pages used to render their full shell (with "—" placeholders and 403 errors) for any
 * signed-in account. The API already refuses the data; this shows one clear message instead.
 * UX only: authorization stays on the API.
 */

import Link from "next/link";
import { ShieldAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useAuth } from "@/lib/auth-context";

export function AdminGate({ children }: { children: React.ReactNode }) {
  const { user, isAdmin } = useAuth();
  // No user yet: AuthGuard (outside) owns the loading and redirect states.
  if (!user || isAdmin) return <>{children}</>;
  return (
    <Card className="mx-auto mt-8 max-w-xl">
      <CardContent className="space-y-4 p-8 text-center">
        <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-danger/10 text-danger">
          <ShieldAlert className="h-6 w-6" />
        </span>
        <h2 className="text-lg font-semibold">Administrators only</h2>
        <p className="text-sm text-muted-foreground">
          This area is only for the people who run ComputeCoin. Your account is not an admin account. Go back to your console to keep working, or ask an admin to give you access.
        </p>
        <Button asChild>
          <Link href="/dashboard">Back to your console</Link>
        </Button>
      </CardContent>
    </Card>
  );
}
