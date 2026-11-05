# U11 Services and endpoints (design note)

Status: code written, not yet run or tested. ADR-0006 section "Services", WP-U11 (U11.1 to U11.3).

## What it is

A **Service** is a long-running endpoint: a Kit (a digest-pinned, signed, approved image; BYOC images come with U9)
with min and max replicas, GPU needs, a health check path and optional scale to zero. The customer calls one stable
URL. Hosts stay outbound-only: the agent pulls requests and pushes responses.

Off by default: `SERVICES_ENABLED=false` (fail closed). Privacy tiers (sealed, sealed_pro, confidential) are refused
(`privacy_tier_not_supported_for_services`); they come with U18.

## Replicas are ordinary jobs

A replica is one ordinary Job Spec v2 (Kit) job, placed by the v2 scheduler (`scheduler.schedule_job`, which calls
`scheduler_v2` for fit and cache affinity), with hold, lease, heartbeat, result and settlement all unchanged.

To keep every hold bounded, a replica job lives for one **window** (`SERVICE_REPLICA_WINDOW_SECONDS`, default 3600).
Before a window ends the controller starts a successor; at the window end the old replica is told to drain, finish
in-flight requests and post its normal job result with the metered `duration_seconds`. Billing is therefore the
existing `settle_job` per replica job: GPU-seconds per replica, nothing new in ledger, pricing or billing.

The claim response carries `v2.service` (additive, only for replica jobs). The agent branches on it in `run_one` and
runs `service_agent.run_service` instead of a batch run.

## Request path (pull-based queue)

1. `POST /v1/services/{id}/invoke?path=/x` (raw body, size capped) or, for `kind=inference`,
   `POST /v1/services/{id}/v1/chat/completions` (non-streaming) creates a `ServiceRequest` (queued) and waits up to
   the request timeout for it to finish. `Idempotency-Key` dedupes customer retries.
2. A replica agent calls `POST /v1/agents/services/replicas/{replica_id}/poll` (machine-signed). The API holds the call
   up to `SERVICE_POLL_MAX_WAIT_SECONDS`, hands out at most one queued request (`FOR UPDATE SKIP LOCKED`), and also
   treats the poll as a replica heartbeat. The reply may say `stop` (draining).
3. The agent forwards to its container over loopback or an internal network and posts
   `POST /v1/agents/services/requests/{request_id}/response` (machine-signed, response size capped). First writer
   wins; a late response from a replica that lost the request is refused (409).
4. Request path, status and size are validated both sides. The agent forwards ONLY to the declared service port and
   a path that passes the same check (no `..`, no scheme, no host).

Routing only considers replicas that are `healthy` with a fresh heartbeat (a request is simply taken by whichever
healthy replica polls).

## Autoscaling controller (worker sweep, own try/except and timer)

Every `SERVICE_SWEEP_INTERVAL_SECONDS` per service (one transaction and one try/except each):

1. Failover first (below).
2. Desired replicas: scale **up** by one when queued requests per healthy replica is at or above
   `SERVICE_SCALE_UP_QUEUE_PER_REPLICA`, or the oldest queued request waited longer than
   `SERVICE_SCALE_UP_LATENCY_SECONDS`. Scale **down** by one when idle (no request for
   `SERVICE_SCALE_DOWN_IDLE_SECONDS`) and above min. If `scale_to_zero` is on and no request for
   `SERVICE_IDLE_SCALE_TO_ZERO_SECONDS`, desired is 0. A queued request with zero replicas wakes it to 1 (cold start).
3. Clamp to `[min, max]` (service values, themselves capped by `SERVICE_MAX_REPLICAS_LIMIT`), then by the spend cap:
   no replica is started if spent so far plus the live replicas' authorised amounts plus the new replica's
   `max_cost` would exceed the service's `max_total_cost`. A cooldown (`SERVICE_SCALE_COOLDOWN_SECONDS`) separates
   scale actions. Reaching the cap sets `capped=true` on the service view; running replicas finish their window and
   are not renewed.
4. Starting a replica = create the job (same flow as `POST /v1/jobs/v2`, with `spend_guard`, `schedule_job`). Two
   replicas of one service prefer different machines (the second is placed outside machines already hosting a live
   replica when one exists, same idea as failover; a single host still works). That is the "second host".

## Failover within 60 seconds

`SERVICE_HEARTBEAT_TIMEOUT_SECONDS` (default 30) plus the sweep interval (10) stays under 60 s.
A replica whose last poll or heartbeat is older than the timeout becomes `dead`: it leaves routing at once; its
assigned requests go back to `queued` with `attempts+1` (at most one retry: after the second attempt the request is
failed with `replica_lost`); its job is left to the existing lease recovery (no retries for replicas: the controller
starts the replacement, preferring a different machine, in the same sweep). A request keeps its id and idempotency
key throughout, and only the first accepted response counts.

Retried requests may reach the service twice. Services must tolerate that (documented in the API reference); a
customer can send `X-Service-No-Retry: 1` to get `replica_lost` instead of a retry.

## Billing and reconciliation

Metering is per replica job (`duration_seconds` posted by the agent in the job result; the existing minimum billable
duration applies). No ledger, pricing or billing code is changed.
`service_runtime.reconcile_service(db, service_id)` is read-only. It compares, over replicas whose job settled:
the sum of replica metered seconds with the sum of `usage_records.actual_seconds`, and reports billed minor units
(`usage_records.actual_cost_minor`). It also reports the server-observed uptime (first healthy to last heartbeat)
against the agent's claim and flags a replica claiming more than `SERVICE_METER_TOLERANCE` over what the platform saw.
`GET /v1/services/{id}/reconciliation` returns it to the owner.

### Per-request billing option (`SERVICE_BILLING_MODE`)

`uptime` (default) is the behaviour above. `per_request` (owner decision) accrues one `service_request_charges` row per
completed (`done`, never failed) request: service time (assigned to completed, capped at the request timeout) times the
replica's GPU count, unique per request so accrual is idempotent. No ledger code is added: when the replica job's result
arrives, `apply_per_request_metering` closes the replica's batch (`batch_key = replica:<id>`, idempotent) and sets
`job.metrics.duration_seconds` to the billable request seconds (sum of service seconds times
`SERVICE_PER_REQUEST_RATE_MULTIPLIER`, never more than the true uptime, which is kept as `metrics.uptime_seconds`). The
existing `settle_job` then charges that at the quote's per-GPU-second rate (it multiplies by gpu_count) with its normal
hold, capture and release; a failed or cancelled replica job releases its whole hold and charges nothing. One batch is one
replica window. Concurrent requests overlap in time, so the cap at uptime prevents charging more than uptime billing.
`reconcile_service` adds per-request totals (`request_charged_seconds`, `request_gpu_seconds`, `request_charges`) and
flags `request_total_differs_from_metered` and `request_charges_not_in_batch` (a request finishing after its batch closed
is not billed). If the metering hook fails it is logged and the job bills uptime (the safe-hook rule); watch the flag.

### Replica start timeout and concurrency

A replica in `starting` that never became healthy within `SERVICE_REPLICA_START_TIMEOUT_SECONDS` (default 600) is marked
dead (`start_timeout`), its job cancelled with the hold released in full, and the same sweep starts a replacement that
avoids that host for the next hour. `services.max_concurrency` (default 1, cap 64 and `SERVICE_MAX_CONCURRENCY_LIMIT`)
is delivered in the claim; the agent serves that many requests in parallel in a bounded thread pool, each with its own
timeout (time left to the request deadline), without blocking the job heartbeat; the API also stops handing a replica work
beyond its `in_flight` limit. Migration 0041 (revises 0040) adds the column and the charges table.

## OWNER DECISIONS (settings, conservative defaults)

| Setting | Default | Why it is the owner's call |
|---|---|---|
| `SERVICES_ENABLED` | false | launch gate |
| `SERVICE_MAX_REPLICAS_LIMIT` | 4 | platform cap on any service's max replicas |
| `SERVICE_MAX_PER_CUSTOMER` | 3 | live services per customer |
| `SERVICE_DEFAULT_SPEND_CAP` | "50.00" | cap used when a service sets none; hard maximum `SERVICE_MAX_SPEND_CAP` "500.00" |
| `SERVICE_REPLICA_WINDOW_SECONDS` | 3600 | bounds each hold |
| `SERVICE_REPLICA_MAX_COST` | "10.00" | authorised amount per replica window when the spec sets none |
| scale up/down thresholds, cooldown, idle times | see config | autoscaling policy |
| `SERVICE_BILLING_MODE` | uptime | `uptime` or `per_request` |
| `SERVICE_PER_REQUEST_RATE_MULTIPLIER` | "1" | multiplier on the existing per-GPU-second rate for request seconds |
| `SERVICE_REPLICA_START_TIMEOUT_SECONDS` | 600 | replace a replica that never became healthy |

## Not done / limits

- Streaming responses, websockets, and service-to-service calls.
- Sharing a service with other users or API keys other than the owner's session.
- BYOC images (U9) and privacy tiers (U18).
- Cross-process request wake-up uses short DB polling (0.5 s), not a message bus.
- The agent's container networking (internal docker network, reach by container IP) needs a real host to verify.
