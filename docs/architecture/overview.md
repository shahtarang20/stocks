# ComputeCoin — Architecture Overview

Status: Phase 0/1. This document maps the components and the lifecycle of a job,
and marks what is built vs planned.

## Component map

| Component | Responsibility | Phase 1 status |
| --- | --- | --- |
| **web** | Customer + provider dashboard (projects, keys, jobs, billing). | Planned |
| **api** | FastAPI gateway: auth, projects, API keys, job submission, balances. Source of truth for requests. | Partially built (auth/projects skeletoned via SDK contract) |
| **scheduler** | Matches jobs to provider machines; holds short-lived leases (Redis); makes atomic reservations against balances (Postgres). | Planned |
| **job-worker** | Backend worker that drives a job through its state machine, talks to the assigned provider-agent, collects artifacts. | Planned |
| **billing** | Double-entry ledger, reservations capture/release, invoices. Postgres, exact NUMERIC. | Planned |
| **verification** | Benchmarks/attests provider capabilities; only this may mark capabilities VERIFIED. | Planned |
| **provider-agent** | Runs on provider hardware: detect / enroll / signed heartbeat (this repo). Job execution NOT yet implemented. | **Built (skeleton)** |
| **contracts** | On-chain settlement / token (future). | Planned |
| **SDKs** | Typed Python + TypeScript clients. | **Built (skeleton)** |

Datastores (see ADR-0001): **PostgreSQL** (authoritative), **Redis** (cache /
rate-limit / leases), **S3-compatible object storage** (artifacts; MinIO local /
Supabase Storage hosted).

## Trust boundaries (high level)

```
 Customer ──TLS──▶ [api] ──▶ [scheduler] ──▶ [job-worker] ──signed──▶ [provider-agent]
                     │             │                                      │ (untrusted,
                     ▼             ▼                                      │  3rd-party HW)
                 [Postgres]    [Redis leases]                       [hardened sandbox]
                     │
                     ▼
              [object storage] (artifacts)
```

Every boundary where data crosses from a less- to more-trusted zone (or onto
provider hardware) is enumerated in `docs/security/threat-model.md`.

## Job lifecycle — finite state machine

A job is an explicit FSM; transitions are the only way state changes, and each
transition is audited.

```
CREATED
  └▶ QUEUED            (accepted, funds reserved against balance)
       └▶ SCHEDULED    (matched to a machine; lease acquired)
            └▶ RUNNING (provider-agent reports workload started)
                 ├▶ COMPLETED   (artifacts stored; reservation captured → ledger)
                 ├▶ FAILED      (error; reservation released or partial-captured)
                 └▶ CANCELLED   (customer/admin cancel; reservation released)
  └▶ REJECTED          (validation/quota/funds failure before QUEUED)
EXPIRED                (lease/timeout exceeded from SCHEDULED/RUNNING → FAILED path)
```

Rules:

- **Funds**: reserved on `CREATED → QUEUED`, **captured** only on
  `→ COMPLETED`, **released** on `FAILED/CANCELLED/EXPIRED`. Reservation and
  ledger writes are one Postgres transaction (ADR-0001).
- **Leases**: `SCHEDULED/RUNNING` hold a TTL lease in Redis; a dead
  worker/agent lets the lease expire → job moves to `EXPIRED → FAILED` and funds
  release. Postgres remains authoritative for the final state.
- **Capabilities**: the machine's reported capabilities are `DETECTED` until the
  verification component benchmarks/attests them (never trusted blindly).

## Built vs planned in Phase 1

- **Built (skeleton, tested):** provider-agent (detect/enroll/heartbeat),
  Python SDK, TypeScript SDK, Phase-0 docs.
- **Contract-level (shaped, not served):** auth/projects/api-key/inference
  endpoints exist as SDK methods; backend may return availability errors until
  implemented.
- **Planned:** scheduler, job-worker, billing/ledger, verification, web,
  on-chain contracts, job execution + sandboxing in the agent.
