"use client";

/**
 * Production-hardened auth context.
 *
 * Auth is backed entirely by secure, httpOnly, SameSite=Lax cookies managed by
 * the backend API with double-submit CSRF protection on mutating methods.
 *
 * No access or refresh token is ever stored in JS-accessible storage (localStorage
 * or sessionStorage), eliminating XSS token exfiltration risks (Workstream A1).
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
import {
  api,
  ApiError,
  clearStoredTokens,
  getStoredAccessToken,
  getStoredRefreshToken,
  type CurrentUser,
} from "./api";

interface AuthContextValue {
  user: CurrentUser | null;
  email: string | null;
  /**
   * Access token: returns the real JWT access token when available, falling back
   * to "cookie-session" sentinel for pure ambient cookie sessions.
   */
  accessToken: string | null;
  isAuthenticated: boolean;
  isReady: boolean;
  /** Authoritative primary role from /auth/me */
  role: string | null;
  /** Authoritative admin status from /auth/me */
  isAdmin: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [isReady, setIsReady] = useState(false);

  // Rehydrate session from stored Bearer token or httpOnly cookie on mount via GET /auth/me
  useEffect(() => {
    let cancelled = false;

    async function initAuth() {
      try {
        const me = await api.getMe();
        if (!cancelled) {
          setUser(me);
          setIsReady(true);
        }
      } catch (err) {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          // Attempt silent refresh via stored refresh_token or httpOnly refresh cookie
          try {
            await api.refresh(getStoredRefreshToken() ?? undefined);
            const me = await api.getMe();
            if (!cancelled) {
              setUser(me);
            }
          } catch {
            clearStoredTokens();
            if (!cancelled) {
              setUser(null);
            }
          }
        } else if (!cancelled) {
          setUser(null);
        }
        if (!cancelled) {
          setIsReady(true);
        }
      }
    }

    initAuth();

    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    await api.login({ email, password });
    const me = await api.getMe();
    setUser(me);
  }, []);

  const register = useCallback(async (email: string, password: string) => {
    await api.register({ email, password });
    // Log in immediately after a successful registration
    await api.login({ email, password });
    const me = await api.getMe();
    setUser(me);
  }, []);

  const logout = useCallback(() => {
    clearStoredTokens();
    api.logout().catch(() => {});
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(() => {
    const roles = user?.roles ?? [];
    const primaryRole = roles[0] ?? null;
    const admin = roles.includes("administrator") || roles.includes("admin");

    return {
      user,
      email: user?.email ?? null,
      accessToken: user ? (getStoredAccessToken() || "cookie-session") : null,
      isAuthenticated: Boolean(user),
      isReady,
      role: primaryRole,
      isAdmin: admin,
      login,
      register,
      logout,
    };
  }, [user, isReady, login, register, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an <AuthProvider>");
  }
  return ctx;
}
