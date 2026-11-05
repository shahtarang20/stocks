# Load test baseline and capacity plan

STATUS: UNMEASURED. No load test has been run. Every cell below is intentionally empty; do not
fill in numbers that were not measured. Fill this in from a real run of `tests/load/` and record
the exact command, commit and machine.

## Run record

| Field | Value |
|---|---|
| Date | |
| Git commit | |
| Command (users, spawn rate, duration) | |
| Stack | local docker-compose, Postgres + Redis (versions: ) |
| Machine (CPU model/cores, RAM, disk, OS) | |
| API workers / DB pool size / Redis | |
| Seed data (machines, jobs, users) | |
| Rate limits active (per-user inference limit etc.) | |

## Results per scenario

| Scenario / request | Requests | p50 ms | p95 ms | p99 ms | Error rate | Req/s |
|---|---|---|---|---|---|---|
| GET /v1/marketplace/machines | | | | | | |
| GET /v1/marketplace/machines/{id} | | | | | | |
| POST /auth/login | | | | | | |
| POST /v1/jobs | | | | | | |
| GET /v1/jobs/{id} | | | | | | |
| POST /v1/agents/heartbeat | | | | | | |
| POST /v1/agents/jobs/claim | | | | | | |
| POST /v1/chat/completions | | | | | | |
| Aggregated | | | | | | |

Notes: record 429s (rate limiting) separately from real errors; the inference scenario counts 429
as failure on purpose. Agent users share one machine, so the agent rows measure signature
verification and claim locking, not machine fan-out.

## Regression thresholds

Agreed values go in `tests/load/thresholds.json` (null = not enforced). Owner to approve:

| Metric | Agreed limit | Basis (baseline run) |
|---|---|---|
| p95 per request | | |
| p99 per request | | |
| Error rate | | |
| Min throughput | | |

## Capacity plan (skeleton, to complete after the baseline)

| Component | Signal that triggers scaling | Threshold (from baseline) | Action |
|---|---|---|---|
| API (uvicorn workers/instances) | CPU, request p95, queue depth | | Add workers or instances |
| Worker (settlement, reconciliation, stake sync) | Job/queue lag | | Add worker replicas |
| Postgres | Connections in use, slow queries, CPU, lock waits | | Raise pool, add indexes, read replica, bigger instance |
| Redis | Memory, ops/s, latency | | Bigger instance |
| Agent claim path | Claim latency, lock contention | | Review claim query and indexes |

Open questions: expected users, machines and jobs/hour at launch (owner input); target SLOs.
