"use client";

import { useState, useEffect, useRef, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { AuthProvider, useAuth } from "@/lib/auth-context";

/**
 * Query keys are not scoped to a user, so the cache must not outlive the signed-in account:
 * after a log out (or a session expiry) the next person to sign in on this tab would otherwise be
 * shown the previous account's jobs, balance and keys until each query refetched.
 */
export function AuthCacheScope() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const userId = user?.id ?? null;
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (previous.current !== null && previous.current !== userId) queryClient.clear();
    previous.current = userId;
  }, [userId, queryClient]);
  return null;
}

export function Providers({ children }: { children: ReactNode }) {
  useEffect(() => {
    // Defensively unregister any stale service workers from prior local apps on port 3000
    if (typeof window !== "undefined" && "serviceWorker" in navigator) {
      navigator.serviceWorker.getRegistrations().then((registrations) => {
        for (const reg of registrations) {
          reg.unregister().catch(() => {});
        }
      }).catch(() => {});
    }
  }, []);
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            refetchOnWindowFocus: false,
            staleTime: 30_000,
          },
        },
      }),
  );

  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem
      disableTransitionOnChange
    >
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthCacheScope />
          {children}
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
