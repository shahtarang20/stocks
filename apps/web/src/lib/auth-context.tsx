"use client";

/**
 * Phase-1 auth context.
 *
 * ⚠️ SECURITY — PHASE 1, DEV-ONLY (MUST CHANGE BEFORE PRODUCTION):
 * We keep the access token in memory (React state) and persist the refresh
 * token + access token in localStorage so a page reload keeps the session.
 * This is deliberately simple for Phase-1 development.
 *
 * TODO(security): Tokens MUST move to secure, httpOnly, SameSite cookies set by
 * the backend. localStorage is readable by any JS on the page and is vulnerable
 * to XSS token theft. Real authorization is enforced server-side on the API;
 * the client guard here is a UX convenience only, NOT a security boundary.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError, type TokenPair } from "./api";

const STORAGE_KEY = "computecoin.auth.v1"; // dev-only, see security note above.

interface StoredAuth {
  accessToken: string;
  refreshToken: string;
  email: string;
}

interface AuthContextValue {
  email: string | null;
  accessToken: string | null;
  isAuthenticated: boolean;
  isReady: boolean;
  /** Best-effort role from the JWT. UX only — real RBAC is server-side. */
  role: string | null;
  /** Client-side admin hint (UX only). The API enforces RBAC on every call. */
  isAdmin: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

/**
 * Decode a JWT's claims WITHOUT verifying the signature. This is a UX-only hint
 * (e.g. to reveal an admin nav link) — the server verifies the token and
 * enforces authorization on every request. Never trust this for access control.
 */
function decodeJwtClaims(token: string | null | undefined): Record<string, unknown> | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length < 2) return null;
  try {
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const json =
      typeof atob === "function"
        ? atob(padded)
        : Buffer.from(padded, "base64").toString("binary");
    const decoded = decodeURIComponent(
      Array.from(json)
        .map((c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2))
        .join(""),
    );
    return JSON.parse(decoded) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** Pull a role string out of common JWT claim shapes (UX-only hint). */
function roleFromClaims(claims: Record<string, unknown> | null): string | null {
  if (!claims) return null;
  const single = claims.role ?? claims.user_role;
  if (typeof single === "string") return single;
  const many = claims.roles ?? claims.scopes ?? claims.scope;
  if (Array.isArray(many)) return many.map(String).join(" ");
  if (typeof many === "string") return many;
  return null;
}

/** Decide whether the decoded claims hint at admin/operator access (UX only). */
function isAdminFromClaims(claims: Record<string, unknown> | null): boolean {
  if (!claims) return false;
  if (claims.is_admin === true || claims.is_staff === true) return true;
  const role = roleFromClaims(claims)?.toLowerCase() ?? "";
  return /\b(admin|administrator|operator|support|security|staff)\b/.test(role);
}

const AuthContext = createContext<AuthContextValue | null>(null);

function readStored(): StoredAuth | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredAuth) : null;
  } catch {
    return null;
  }
}

function writeStored(value: StoredAuth | null) {
  if (typeof window === "undefined") return;
  try {
    if (value) {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    /* storage may be unavailable (private mode) — session stays in memory. */
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<StoredAuth | null>(null);
  const [isReady, setIsReady] = useState(false);

  // Rehydrate from localStorage on mount (dev-only persistence).
  useEffect(() => {
    setState(readStored());
    setIsReady(true);
  }, []);

  const applyTokens = useCallback((tokens: TokenPair, email: string) => {
    const next: StoredAuth = {
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token,
      email,
    };
    setState(next);
    writeStored(next);
  }, []);

  const login = useCallback(
    async (email: string, password: string) => {
      const tokens = await api.login({ email, password });
      applyTokens(tokens, email);
    },
    [applyTokens],
  );

  const register = useCallback(
    async (email: string, password: string) => {
      await api.register({ email, password });
      // Convenience: log in immediately after a successful registration.
      const tokens = await api.login({ email, password });
      applyTokens(tokens, email);
    },
    [applyTokens],
  );

  const logout = useCallback(() => {
    setState(null);
    writeStored(null);
  }, []);

  // Attempt a one-shot refresh on mount if we have a refresh token.
  useEffect(() => {
    if (!isReady) return;
    const stored = state;
    if (!stored?.refreshToken) return;
    let cancelled = false;
    (async () => {
      try {
        const tokens = await api.refresh(stored.refreshToken);
        if (!cancelled) applyTokens(tokens, stored.email);
      } catch (err) {
        // If refresh is rejected (expired/invalid), drop the session silently.
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          if (!cancelled) {
            setState(null);
            writeStored(null);
          }
        }
        // Network errors are ignored — keep the stored session optimistically.
      }
    })();
    return () => {
      cancelled = true;
    };
    // Run only once after rehydration.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReady]);

  const value = useMemo<AuthContextValue>(() => {
    const claims = decodeJwtClaims(state?.accessToken);
    return {
      email: state?.email ?? null,
      accessToken: state?.accessToken ?? null,
      isAuthenticated: Boolean(state?.accessToken),
      isReady,
      role: roleFromClaims(claims),
      isAdmin: isAdminFromClaims(claims),
      login,
      register,
      logout,
    };
  }, [state, isReady, login, register, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an <AuthProvider>");
  }
  return ctx;
}
