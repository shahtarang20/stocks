# ComputeCoin

A decentralized AI GPU computing marketplace that connects independent GPU
providers with developers who need compute. This repository is built
**incrementally, with working, tested functionality** — not a single untested
code dump, and with **no fabricated jobs, capacity, benchmarks, or token
metrics**.

> **Status: Phase 0–1 (foundation).** See
> [`docs/operations/implementation-checklist.md`](docs/operations/implementation-checklist.md)
> for exactly what is built vs. planned. Anything not yet connected is labelled
> as such. Demo/marketplace data is always marked **DEMO** and is never
> presented as live infrastructure.

## Architecture at a glance

- **Postgres** is the transactional source of truth (ACID, double-entry ledger,
  atomic resource reservations, FK/constraint integrity). MongoDB was evaluated
  and rejected for the money/correctness core — see
  [ADR-0001](docs/architecture/ADR-0001-database.md). Hosted via **Supabase**
  free tier (Postgres + S3 storage) while we keep our **own FastAPI auth**.
- **Redis** for caching, rate limiting, and short-lived leases/quotes.
- **S3-compatible object storage** (MinIO locally, Supabase Storage hosted) for
  datasets, logs, and artifacts.
- **FastAPI** backend; **Next.js** frontend; a separately-installable
  **provider agent**; Python & TypeScript **SDKs**.

## Repository layout

```
apps/        web (Next.js), api (FastAPI), admin
services/    scheduler, job-worker, billing, verification
provider-agent/   installable GPU provider agent (computecoin_agent)
sdk/         python, typescript client SDKs
contracts/   Solidity (future token phase — testnet only, not yet)
packages/    shared-types, ui
infrastructure/   docker, monitoring, deployment
docs/        architecture (ADRs), api, operations, security, tokenomics
tests/       integration, security, end-to-end
```

## Quick start (local, CPU-only — no GPU required)

Prereqs: Docker, Python 3.12, Node 22.

```bash
cp .env.example .env            # fill in secrets (openssl rand -hex 32)

# Option A — everything in Docker:
make up                         # db, redis, minio, api, web
# API:  http://localhost:8000/health     Web: http://localhost:3000

# Option B — backing services in Docker, apps on host (faster iteration):
make infra                      # db, redis, minio
make api-install api-migrate api-run     # in one terminal
make web-install web-dev                 # in another
```

Run the tests:

```bash
make api-test        # backend pytest suite
make agent-test      # provider-agent pytest suite
make web-check       # frontend typecheck
```

## Security posture (release requirement, not an afterthought)

- Argon2id password hashing; short-lived access JWTs + **rotating** refresh
  tokens with reuse detection; server-side RBAC; API keys stored **hashed**
  (prefix shown, secret returned once); audit logs for sensitive actions.
- Customer workloads never run in the API process, never get the Docker socket
  or host mounts, and ordinary containers are **not** claimed to guarantee
  complete isolation. See
  [`docs/security/threat-model.md`](docs/security/threat-model.md) and
  [ADR-0003](docs/architecture/ADR-0003-isolation.md).

## Honesty commitments

- No fabricated provider listings, benchmarks, utilization, ratings, or pricing.
- No token price/market-cap/volume shown anywhere; the future token is never
  marketed as a guaranteed-appreciation investment.
- Failed/cancelled jobs are never billed as successful completions.

## License

TBD.
