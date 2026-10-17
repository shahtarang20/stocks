# ADR-0001: Datastores — PostgreSQL as transactional source of truth

- Status: Accepted
- Date: 2026-10-16
- Phase: 0

## Context

ComputeCoin is a compute marketplace that moves money. The core domain has
multi-row invariants that must hold atomically:

- a **double-entry ledger** (every debit has a matching credit; the sum is zero),
- **atomic reservations** of provider capacity against a customer balance
  (reserve, then capture or release — no double-spend),
- strong **referential integrity** between users, projects, API keys, jobs,
  providers, machines, reservations and ledger entries.

Getting any of these wrong corrupts balances or lets jobs run without being
paid for. We need a datastore whose correctness guarantees we can lean on.

## Decision

**PostgreSQL is the single transactional source of truth** for all
money/correctness-critical state.

We rely specifically on:

- **ACID transactions** so a reservation + ledger write either both commit or
  both roll back.
- **`NUMERIC`/`DECIMAL`** for exact money math (never floating point).
- **Foreign keys + `CHECK` constraints** to make illegal states unrepresentable
  (e.g. ledger entries must reference a real account; balances non-negative).
- **`SELECT ... FOR UPDATE`** (and `SERIALIZABLE`/`REPEATABLE READ` where
  needed) to serialize concurrent reservations against the same balance.
- **Unique/partial indexes** for idempotency keys and one-active-lease
  invariants.

Supporting stores, each for a job Postgres should **not** do:

- **Redis** — ephemeral, non-authoritative: response cache, rate-limit counters,
  short-lived scheduler **leases** (with TTL), and pub/sub for live updates. A
  lost Redis key must never lose money; anything durable is reconciled from
  Postgres.
- **S3-compatible object storage** — artifacts (job inputs/outputs, logs, model
  weights). **MinIO** locally, **Supabase Storage** hosted. Postgres stores only
  the metadata/pointers and checksums.

### Hosting

Use the **Supabase free tier** as the hosted **Postgres + Storage** provider for
Phase 1, **while keeping our own FastAPI-based auth** (see ADR-0002) rather than
adopting Supabase Auth. We treat Supabase as managed Postgres + an S3-compatible
bucket, not as an application platform, so we are not locked in and can migrate
to any Postgres host later.

## Alternatives considered

### MongoDB — REJECTED for the money/correctness core

MongoDB was considered because of developer familiarity and flexible documents.
Rejected for the transactional core because:

- **Multi-row invariants**: our correctness depends on relationships spanning
  many rows/tables (ledger balance = sum of entries; a reservation references a
  balance and a job). Enforcing these needs real FKs and cross-row constraints,
  which a document store does not provide natively. Multi-document transactions
  exist but are the exception, not the model, and carry sharp edges.
- **FKs / referential integrity**: no enforced foreign keys; orphaned
  references become an application concern and eventually a data-integrity bug.
- **`SELECT FOR UPDATE`**: no direct equivalent for the pessimistic row locking
  we use to serialize concurrent reservations safely.
- **Exact `NUMERIC`**: BSON's `Decimal128` exists but the broader ecosystem and
  defaults nudge toward doubles; Postgres `NUMERIC` with constraints is the
  safer default for money.

MongoDB (or another document store) remains a reasonable option for
non-authoritative, schema-loose data later (e.g. opaque job specs, provider
telemetry blobs) — but **not** the ledger, balances, or reservations.

### Supabase Auth — deferred (see ADR-0002)

We keep our own auth to retain full control over token rotation, reuse
detection, RBAC, and API-key hashing.

## Future

- **Time-series / TimescaleDB** is the planned home for **high-volume telemetry**
  (per-second GPU utilization, temperatures, throughput). Keeping that firehose
  out of the OLTP tables protects the transactional core. Not built in Phase 1.

## Consequences

- We commit to SQL migrations and schema discipline from day one.
- Money logic lives in transactions with explicit locking; engineers must
  understand isolation levels.
- Two extra moving parts (Redis, object storage) that must each be treated as
  non-authoritative and reconcilable from Postgres.
