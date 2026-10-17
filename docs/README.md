# ComputeCoin — Documentation

Phase-0 deliverables for the ComputeCoin decentralized GPU compute marketplace.
These docs are honest about what is built vs planned; see the overview and the
implementation checklist for status.

## Architecture

- [Overview](architecture/overview.md) — component map, trust boundaries, and
  the job finite state machine (CREATED → … → COMPLETED).
- [ADR-0001: Database](architecture/ADR-0001-database.md) — PostgreSQL as the
  transactional source of truth; Redis; S3-compatible storage; why MongoDB was
  rejected for the money core; Supabase as hosted Postgres+Storage; Timescale
  for future telemetry.
- [ADR-0002: Auth](architecture/ADR-0002-auth.md) — Argon2id, short access JWT +
  rotating opaque refresh tokens with reuse detection, server-side RBAC, hashed
  API keys, audit logs; why not Supabase Auth.
- [ADR-0003: Isolation](architecture/ADR-0003-isolation.md) — hardened
  containers now, microVMs/dedicated hosts for strong isolation; containers are
  not a complete boundary.

## Security

- [Threat model](security/threat-model.md) — STRIDE-style per component (API,
  scheduler, provider-agent, artifacts, ledger, future contracts) with explicit
  residual risk.

## Operations

- [Implementation checklist](operations/implementation-checklist.md) — the
  10-phase plan as a tracked checklist (Phase 0 done, Phase 1 in progress).

## Code (elsewhere in the repo)

- `provider-agent/` — the GPU provider agent (detect / enroll / signed
  heartbeat). Built & tested skeleton; does **not** run jobs yet.
- `sdk/python/` — `computecoin` Python client.
- `sdk/typescript/` — `@computecoin/sdk` TypeScript client.
