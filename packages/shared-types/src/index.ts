// ============================================================================
// @computecoin/shared-types
// Canonical TypeScript types shared across web, admin, and the TS SDK.
// Keep these in sync with the backend Pydantic schemas (apps/api/app/schemas).
// ============================================================================

/** Business roles. Authorization is enforced SERVER-SIDE; this is for UI only. */
export type Role =
  | "guest"
  | "customer"
  | "provider"
  | "support_operator"
  | "security_operator"
  | "administrator";

export type UserStatus = "active" | "suspended";

export interface User {
  id: string;
  email: string;
  emailVerified: boolean;
  status: UserStatus;
  roles: Role[];
  createdAt: string;
}

export interface AuthTokens {
  accessToken: string;
  refreshToken: string;
  tokenType: "bearer";
  expiresIn: number;
}

export interface Project {
  id: string;
  ownerId: string;
  orgId: string | null;
  name: string;
  createdAt: string;
}

/** The full secret is returned exactly once at creation time. */
export interface ApiKeyCreated {
  id: string;
  name: string;
  prefix: string;
  secret: string; // shown ONCE — never stored client-side long term
  createdAt: string;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  lastUsedAt: string | null;
  revokedAt: string | null;
  createdAt: string;
}

/** Job finite-state machine (see docs/architecture/overview.md). */
export type JobStatus =
  | "created"
  | "validating"
  | "queued"
  | "matched"
  | "reserved"
  | "running"
  | "verifying"
  | "completed"
  | "failed"
  | "cancelled"
  | "timed_out"
  | "retry_pending"
  | "disputed";

/** Distinguishes advertised vs detected vs benchmarked vs available capacity. */
export type VerificationStatus =
  | "self_reported"
  | "detected"
  | "benchmarked"
  | "verified";

export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail?: string;
  requestId?: string;
}
