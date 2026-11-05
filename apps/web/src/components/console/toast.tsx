"use client";

/**
 * Minimal toast system for the console (no external dependency).
 *
 * `useToast()` is safe to call anywhere: outside a ToastProvider (for example in
 * isolated component tests) it returns a no-op, so pages never crash for lack of it.
 * Toasts are announced politely to screen readers via an aria-live region.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

type ToastKind = "success" | "error" | "info";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

const NOOP: ToastApi = { success: () => {}, error: () => {}, info: () => {} };
const ToastContext = createContext<ToastApi>(NOOP);

export function useToast(): ToastApi {
  return useContext(ToastContext);
}

const ICONS = { success: CheckCircle2, error: XCircle, info: Info } as const;
const TONES: Record<ToastKind, string> = {
  success: "border-success/40 text-success",
  error: "border-danger/40 text-danger",
  info: "border-border text-muted-foreground",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const timers = useRef<Map<number, number>>(new Map());

  const dismiss = useCallback((id: number) => {
    setItems((list) => list.filter((t) => t.id !== id));
    const handle = timers.current.get(id);
    if (handle !== undefined) {
      window.clearTimeout(handle);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (kind: ToastKind, message: string) => {
      const id = nextId.current++;
      setItems((list) => [...list.slice(-3), { id, kind, message }]);
      // Errors stay a little longer so they can be read.
      const handle = window.setTimeout(() => dismiss(id), kind === "error" ? 8000 : 4500);
      timers.current.set(id, handle);
    },
    [dismiss],
  );

  useEffect(() => {
    const current = timers.current;
    return () => {
      current.forEach((h) => window.clearTimeout(h));
      current.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      success: (m) => push("success", m),
      error: (m) => push("error", m),
      info: (m) => push("info", m),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        role="region"
        aria-label="Notifications"
        aria-live="polite"
        className="pointer-events-none fixed inset-x-0 bottom-0 z-[60] flex flex-col items-center gap-2 p-4 sm:items-end"
      >
        {items.map((t) => {
          const Icon = ICONS[t.kind];
          return (
            <div
              key={t.id}
              className={cn(
                "pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-md border bg-surface px-4 py-3 text-sm shadow-lg",
                TONES[t.kind],
              )}
            >
              <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
              <p className="min-w-0 flex-1 text-foreground">{t.message}</p>
              <button
                type="button"
                aria-label="Dismiss notification"
                onClick={() => dismiss(t.id)}
                className="rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <X aria-hidden="true" className="h-4 w-4" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
