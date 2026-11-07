# ComputeCoin — Architecture Overview

Status: written in Phase 0/1; the component status column below was brought up to
date on 2026-11-07 against the code. This document maps the components and the
lifecycle of a job, and marks what is built vs planned. "Built" means code exists
in this repo; nothing here has been deployed or run in production.

## Component map

| Component | Responsibility | Status |
| --- | --- | --- |
| **web** | Customer + provider dashboard (projects, keys, jobs, billing). | Built (`apps/web`) |
| **api** | FastAPI gateway: auth, projects, API keys, job submission, balances. Source of truth for requests. | Built (`apps/api`: auth, projects, keys, jobs, billing and many more routers) |
| **scheduler** | Matches jobs to provider machines; holds short-lived leases (Redis); makes atomic reservations against balances (Postgres). | Built (`apps/api/app/services/scheduler.py`, `scheduler_v2.py`) |
| **job-worker** | Backend worker that drives a job through its state machine, talks to the assigned provider-agent, collects artifacts. | Built (`apps/api/app/worker.py`) |
| **billing** | Double-entry ledger, reservations capture/release, invoices. Postgres, exact NUMERIC. | Built (`services/ledger.py`, `billing.py`, `settlement.py`) |
| **verification** | Benchmarks/attests provider capabilities; only this may mark capabilities VERIFIED. | Built (`services/verification.py` checks output shape; kit verification runs separately) |
| **provider-agent** | Runs on provider hardware: detect / enroll / signed heartbeat (this repo). Also claims and runs jobs (`executor.py`, `jobrunner.py`). | **Built** |
| **contracts** | On-chain settlement / token. | Built (`contracts/src`: token, settlement, stake vault, governance); deployment is a separate owner step (see `docs/operations/launch-checklist.md`) |
| **SDKs** | Typed Python + TypeScript clients. | **Built** (`sdk/python`, `sdk/typescript`) |

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

## Built vs planned

The original Phase 1 split (provider-agent skeleton, SDKs and docs built; scheduler, job-worker,
billing, verification, web, contracts and agent job execution planned) is out of date: all of
those now have code in this repo (see the table above). What is NOT done is deployment, real
hardware runs, and the owner decisions tracked in `docs/operations/` (for example
`launch-checklist.md` and `proof-of-privacy-remaining-work.md`).
