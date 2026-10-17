# Phase 3 — Marketplace: search, quotes & reservations (AUTHORITATIVE CONTRACT)

Single source of truth for Phase 3. Backend (`apps/api`), web (`apps/web`), and
the SDKs (`sdk/*`) MUST conform exactly. Change here first if a change is needed.

JSON everywhere. Timestamps RFC 3339 UTC. **All money is a decimal STRING with 2
fraction digits** (e.g. `"1.50"`) computed with `Decimal`/`Numeric` — never a
float. Currency for the MVP is `"USDC_CREDITS"` — **simulated test credits**,
clearly labelled as such in the UI; no real payment rail in Phase 3.

## Concepts
- **Marketplace search** is PUBLIC (no auth) — browse real, online, available,
  priced machines. It exposes capability + price + availability, never provider
  PII or secrets.
- **Quote**: a customer-scoped, itemized, time-limited cost estimate for running
  a workload on a specific machine. Price is SNAPSHOTTED at quote time so the
  amount is stable until it expires.
- **Reservation**: atomically holds a capacity slot on a machine for a lease
  window. Converting a quote to a reservation is the step before job execution
  (Phase 4). Reservations MUST NOT oversubscribe a machine's
  `max_concurrent_jobs`.

## Pricing (transparent, deterministic)
```
estimated_hours = estimated_minutes / 60                      # Decimal
gpu_cost        = price_per_hour_snapshot * estimated_hours * gpu_count
platform_fee    = gpu_cost * platform_fee_rate                # rate from pricing_rules (default 0.10)
total           = gpu_cost + platform_fee
```
All quantized to 2 dp (ROUND_HALF_UP). `platform_fee_rate` comes from the active
`pricing_rules` row (seeded default 0.10). The quote response itemizes every line
so the customer sees the fee BEFORE reserving (spec §14).

## New DB tables (portable types only: Uuid, JSON, Numeric, DateTime(tz))
- `pricing_rules` (id, name, platform_fee_rate Numeric, active bool, created_at) — seed one active default (0.10).
- `quotes` (id, customer_id fk, machine_id fk, workload_type, gpu_count int,
  estimated_minutes int, price_per_hour_snapshot Numeric, gpu_cost Numeric,
  platform_fee Numeric, total Numeric, currency, max_authorized_spend Numeric|null,
  status enum[active|expired|reserved|cancelled], expires_at, created_at).
- `resource_reservations` (id, quote_id fk UNIQUE, machine_id fk, customer_id fk,
  slots int, status enum[active|released|expired|consumed], expires_at, created_at,
  released_at|null).

## Capacity rule (THE correctness invariant)
A machine's used slots = SUM(reservations.slots) WHERE machine_id=? AND
status='active' AND expires_at > now(). A reservation for `slots` is allowed iff
`used + slots <= machine.max_concurrent_jobs`. The check-and-insert MUST be
atomic under a row lock (`SELECT ... FOR UPDATE` on the machine row, or an
equivalent serializable transaction) so two concurrent requests for the last slot
yield exactly one success and one `409`. Expired reservations are treated as free
(lazy expiry); a `release` frees immediately.

---

## Endpoints

### GET /v1/marketplace/machines   (PUBLIC)
Query params (all optional): `gpu_model`, `min_vram_mb`, `max_price_per_hour`,
`region`, `workload` (must be in supported_workloads), `verification_status`,
`page` (default 1), `page_size` (default 20, max 100).
Lists only machines that are `status=online` AND `availability=online` AND have a
non-null `asking_price_per_hour` AND are not deregistered.
```
res 200: {
  "items": [ {
    "machine_id": "uuid",
    "gpu_model": "..."|null, "gpu_count": 0, "vram_mb": 0,
    "cpu_model": "...", "cpu_cores": 0, "ram_mb": 0,
    "region": "..."|null,
    "verification_status": "detected",
    "price_per_hour": "1.50", "currency": "USDC_CREDITS",
    "supported_workloads": ["text-generation","batch-inference"],
    "available_slots": 1, "max_concurrent_jobs": 1,
    "reliability": { "completed_jobs": 0, "success_rate": null },   // honest: null until history exists
    "last_heartbeat_at": "..."
  } ],
  "page": 1, "page_size": 20, "total": 1
}
```
`reliability.success_rate` is `null` when there is no completed-job history — do
NOT fabricate a rating. UI shows "No history yet".

### GET /v1/marketplace/machines/{machine_id}   (PUBLIC)
Single machine, same element shape (404 if not listable).

### POST /v1/quotes   (customer JWT)
```
req: { "machine_id": "uuid", "workload_type": "text-generation",
       "estimated_minutes": 60, "gpu_count": 1,
       "max_authorized_spend": "5.00"? }
res 201: {
  "quote_id": "uuid", "machine_id": "uuid", "customer_id": "uuid",
  "workload_type": "...", "gpu_count": 1, "estimated_minutes": 60,
  "currency": "USDC_CREDITS",
  "breakdown": {
    "price_per_hour": "1.50", "estimated_hours": "1.00",
    "gpu_cost": "1.50", "platform_fee_rate": "0.10",
    "platform_fee": "0.15", "total": "1.65"
  },
  "max_authorized_spend": "5.00"|null,
  "status": "active", "expires_at": "...", "created_at": "..."
}
errors: 404 machine not listable/offline; 422 workload not supported by machine
or gpu_count exceeds machine gpus; 409 budget_exceeded (total > max_authorized_spend).
```
Quote TTL default 120s (config QUOTE_TTL_SECONDS). Price is snapshotted.

### GET /v1/quotes/{id} / GET /v1/quotes   (customer JWT, own only — 404 for others')

### POST /v1/quotes/{id}/reserve   (customer JWT)
Atomically reserve capacity for this quote.
```
req: {}  (empty)
res 201: {
  "reservation_id": "uuid", "quote_id": "uuid", "machine_id": "uuid",
  "slots": 1, "status": "active", "expires_at": "...", "created_at": "..."
}
errors:
  409 quote_expired          (quote.status != active or past expires_at)
  409 quote_already_reserved (a reservation already exists for this quote)
  409 no_capacity            (used + slots > max_concurrent_jobs)  <-- the race loser
  404 quote not owned/not found
```
On success the quote.status becomes `reserved`. Reservation lease TTL default
900s (config RESERVATION_TTL_SECONDS).

### POST /v1/reservations/{id}/release   (customer JWT, own only)
Frees the slot. Sets status `released`, released_at=now. Idempotent-ish: releasing
an already-released reservation → 200/204 (no error). → 200 { "reservation_id", "status": "released" }.

### GET /v1/reservations   (customer JWT, own only)
List the customer's reservations with derived status (active/expired/released).

## Isolation
Quotes and reservations are visible/modifiable only by their owning customer
(others → 404). Marketplace search is public and exposes no owner identity.

## Honesty
No fabricated ratings/benchmarks/utilization. `success_rate` null with no
history. Credits are simulated and labelled. A failed/cancelled future job must
never be billed as success (enforced in Phase 5 billing; reservations here do not
charge).
