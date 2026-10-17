"use client";

/**
 * Client-side route guard.
 *
 * NOTE: This is a UX convenience only. Real authorization is enforced
 * server-side on the ComputeCoin API — every protected request must carry a
 * valid Bearer token and is checked by the backend. Do not treat this guard as
 * a security boundary.
 */

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/lib/auth-context";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isReady } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (isReady && !isAuthenticated) {
      const next = encodeURIComponent(pathname || "/dashboard");
      router.replace(`/login?next=${next}`);
    }
  }, [isReady, isAuthenticated, pathname, router]);

  if (!isReady || !isAuthenticated) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Checking your session…
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
