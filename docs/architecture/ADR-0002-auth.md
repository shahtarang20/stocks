# ADR-0002: Authentication & authorization

- Status: Accepted
- Date: 2026-10-16
- Phase: 0

## Context

ComputeCoin holds balances and bills real money, so account takeover is a
direct financial threat. We need auth we fully control: password hashing, token
lifetimes, revocation, RBAC, API keys for programmatic access, and an audit
trail.

## Decision

Build auth ourselves in the FastAPI backend.

### Password hashing — Argon2id

- **Argon2id** (memory-hard, resistant to GPU/ASIC cracking), with tuned
  memory/time/parallelism parameters reviewed periodically.
- Per-user random salt (handled by the Argon2 encoder); never store or log
  plaintext passwords.
- Enforce a minimum password policy and check against known-breached password
  lists where feasible.

### Sessions — short-lived access JWT + rotating opaque refresh tokens

- **Access token**: short-lived (~10–15 min) **JWT**, signed (asymmetric so the
  gateway can verify without the signing key), carrying `sub`, roles, and token
  version. Stateless verification on the hot path.
- **Refresh token**: **opaque, random, stored hashed** server-side (not a JWT),
  long-lived but **rotating** — every refresh issues a new refresh token and
  invalidates the old one.
- **Reuse detection**: refresh tokens are chained by family. If a
  previously-used (rotated-out) refresh token is presented, we treat it as theft
  and **revoke the entire token family**, forcing re-login. This catches stolen
  refresh tokens even when the attacker races the legitimate client.

### Authorization — server-side RBAC

- Roles and permissions evaluated **server-side** on every request; never trust
  a client-supplied role claim beyond what the server itself signed, and
  re-check sensitive actions against the database (role could be revoked
  mid-session). Scopes/roles: `owner`, `admin`, `member`, `billing`,
  `provider`, plus per-project membership.

### API keys — hashed at rest, prefix shown

- API keys are high-entropy random strings. We store **only a hash** (e.g.
  SHA-256 or Argon2id depending on verification cost budget); the **full key is
  shown exactly once** at creation.
- A short non-secret **prefix** (e.g. `cck_live_ab12…`) is stored in clear and
  displayed in dashboards/logs so users can identify keys without exposing the
  secret.
- Keys are scoped to a project, independently revocable, and carry their own
  rate limits.

### Audit logs

- Append-only audit log for security-relevant events: login success/failure,
  refresh reuse detection, role changes, API-key create/revoke, billing
  changes. Stored in Postgres, exportable, retained per policy.

## Why not Supabase Auth

Supabase Auth is convenient, but we need **full control** of the requirements
above:

- rotating opaque refresh tokens **with reuse detection** and family revocation,
- our own **RBAC model** re-checked server-side against live data,
- **API keys hashed** with a prefix-display scheme tied to our projects,
- our **audit log** schema and retention,
- freedom to tune Argon2id parameters and token lifetimes.

Delegating these to a third-party auth provider would mean fitting our money-
critical policy into their model and losing control of revocation semantics. We
still use Supabase as managed **Postgres + Storage** (ADR-0001); we just own the
auth logic on top.

## Consequences

- More code to own and keep secure (threat-modeled in
  `docs/security/threat-model.md`).
- We must operate signing-key rotation and a token-family revocation store.
- Clear upside: we can meet the reuse-detection and RBAC requirements exactly.
