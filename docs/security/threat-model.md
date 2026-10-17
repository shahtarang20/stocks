# ComputeCoin — Threat Model

Status: Phase 0. STRIDE-style, per component. Honest about residual risk. This
is a living document; threats and mitigations are revisited each phase.

STRIDE = Spoofing, Tampering, Repudiation, Information disclosure, Denial of
service, Elevation of privilege.

## Scope & key assets

- **Money**: balances, the double-entry ledger, reservations. Corruption or
  theft here is the worst outcome.
- **Credentials**: passwords, refresh tokens, API keys, machine signing secrets.
- **Customer code & data**: job inputs/outputs, model weights (confidential).
- **Capability truth**: whether a provider really has the GPU it claims.
- **Availability**: the marketplace being usable.

## Trust boundaries

1. Internet → **api** (customers, untrusted).
2. api → **scheduler/billing** → **Postgres/Redis** (internal).
3. control plane → **provider-agent** on **third-party, semi-trusted hardware**.
4. provider-agent → **customer workload sandbox** (mutually untrusting).
5. system → **object storage** (artifacts).
6. future: system → **on-chain contracts**.

---

## 1. API (auth, projects, billing endpoints)

| STRIDE | Threat | Mitigation |
| --- | --- | --- |
| S | Credential stuffing / account takeover | Argon2id, rate-limit + lockout, breached-password checks, MFA (future) |
| S | Stolen refresh token replay | Rotating opaque refresh tokens + **reuse detection** → revoke family (ADR-0002) |
| T | Request tampering / privilege claims | TLS; server-side RBAC re-checked against DB; never trust client role claims |
| R | User denies an action | Append-only audit log of security/billing events |
| I | Leaking keys/PII in logs | Store API keys hashed, show only prefix; scrub secrets from logs |
| D | Request floods | Per-key/IP rate limits (Redis), quotas, autoscaling, WAF |
| E | IDOR / broken object-level authz | Enforce project membership on every object access |

**Residual risk:** zero-day in a dependency; insider with DB access (mitigate
with least-privilege DB roles, audit, backups).

## 2. Scheduler / reservations

| STRIDE | Threat | Mitigation |
| --- | --- | --- |
| T | Double-spend via concurrent reservations | `SELECT FOR UPDATE` + single Postgres txn for reserve+ledger; non-negative CHECK |
| T | Lease stealing / job hijack | Signed, scoped lease tokens; machine identity verified |
| R | Lost accounting on crash | Postgres authoritative; Redis lease TTL → funds released on expiry |
| D | Starvation / scheduling abuse | Fair-share quotas; backpressure; dead-letter for stuck jobs |
| E | Malicious provider claims jobs it can't run | Capability DETECTED only; verification gate; reputation/slashing (future) |

**Residual risk:** a provider that passes verification then degrades hardware;
mitigate with periodic re-verification and result spot-checks.

## 3. Provider-agent (on third-party hardware)

| STRIDE | Threat | Mitigation |
| --- | --- | --- |
| S | Spoofed machine / fake heartbeats | HMAC-signed heartbeats (future: Ed25519); machine-scoped identity |
| S | Stolen enrollment token used fleet-wide | Enrollment token **revocable + machine-scoped**; machine then uses own secret |
| T | Tampered capability reports | Signatures detect tampering; capabilities remain untrusted (DETECTED) until verified |
| I | Local secret theft from the host | Config file 0600 / dir 0700; future: OS keystore / TPM-sealed keys |
| D | Agent spam / resource exhaustion on control plane | Auth + rate-limit heartbeats; reject unsigned |
| E | **Compromised agent attacks host/other tenants** | Agent does NOT execute jobs yet; when it does: non-root, no docker socket, no host mounts, dropped caps, restricted egress (ADR-0003) |

**Residual risk:** the provider owns the hardware and can lie about or snoop the
environment. Self-reported capabilities are never trusted; confidentiality from
the provider needs confidential computing (future). A malicious provider is
assumed, not excluded.

## 4. Artifact system (object storage)

| STRIDE | Threat | Mitigation |
| --- | --- | --- |
| S | Forged artifact pointers | Store checksums + owner in Postgres; verify on read |
| T | Artifact tampering in transit/at rest | TLS; content hashes; server-side encryption |
| I | Cross-tenant artifact access | Per-object ACLs scoped to project; short-lived signed URLs, least privilege |
| D | Storage filling / cost abuse | Per-project quotas, lifecycle expiry of scratch artifacts |
| E | Path traversal / bucket misconfig | Strict key namespacing; deny public buckets; infra policy checks |

**Residual risk:** a leaked signed URL within its TTL; keep TTLs short and scope
tightly.

## 5. Payment / ledger

| STRIDE | Threat | Mitigation |
| --- | --- | --- |
| T | Unbalanced/edited ledger | Double-entry invariant enforced; ledger append-only; entries immutable |
| T | Rounding/precision drift | Exact `NUMERIC`; no floats for money |
| R | Billing disputes | Immutable ledger + audit log + per-job reservation trail |
| I | Exposure of financial data | RBAC (`billing` role), encryption, minimal surface |
| D | Payment-provider outage | Idempotent operations; reconcile from Postgres; retries |
| E | Manipulating capture to run unpaid work | Capture only on verified COMPLETED; reserve-before-run; one txn |

**Residual risk:** external payment-processor fraud/chargebacks; handle via
reconciliation and holds.

## 6. Future smart contracts

| STRIDE | Threat | Mitigation |
| --- | --- | --- |
| T | Re-entrancy / logic bugs draining funds | Audits, formal checks, checks-effects-interactions, limits |
| R | On-chain irreversibility of mistakes | Upgradable/pausable patterns where appropriate; timelocks |
| I | On-chain data is public | Keep sensitive data off-chain; commit only hashes |
| D | Gas griefing / congestion | Gas limits, pull-over-push payments |
| E | Key compromise (multisig/admin) | Multisig, HSM, key rotation, least authority |

**Residual risk:** bridging on-chain settlement with the off-chain ledger is a
new double-spend surface; design the reconciliation carefully before shipping.

---

## Cross-cutting residual risks (stated honestly)

- Container isolation is **not** a hard security boundary (ADR-0003); kernel
  escapes are possible until microVMs/dedicated hosts ship.
- Providers are assumed potentially malicious; until confidential computing,
  customer workloads are observable by the provider.
- Self-reported capabilities can be gamed between verifications.
- Insider threat on the control plane and supply-chain compromise of
  dependencies remain; mitigated, not eliminated.
