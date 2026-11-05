# U15: Pricing units and quotes (design)

Status: **code written, default OFF, NOT approved.** This is money-adjacent code (master plan L3).
**It needs owner approval and an independent review before `PRICING_UNITS_ENABLED` is set to true.**
No test has been run against it. It changes no existing billing, ledger or pricing behaviour.

## Principle (P5)
The customer is always billed metered GPU-seconds. A "unit" (frame, image, minute of video,
1k tokens) is only used to **quote and display**. A quote never authorises more than the job's
`max_cost`, and the real charge path is untouched.

## What it adds (all new, additive)
- `app/services/unit_quotes.py`: quote math, learned correction, persistence helpers.
- `app/models/unit_quotes.py`, migration `0029_unit_quotes` (revises `0028_job_spec_v2`): tables
  `unit_quotes` and `unit_quote_samples`.
- `app/api/routers/unit_quotes.py`: `POST /v1/unit-quotes`, `GET /v1/unit-quotes/{id}`,
  `POST /v1/unit-quotes/{id}/link`. The path is `/v1/unit-quotes`, not `/v1/quotes`, because
  `/v1/quotes` already exists for machine reservation quotes and must not change.
- Settings (all `OWNER DECISION`): `PRICING_UNITS_ENABLED=false`, `UNIT_QUOTE_TTL_SECONDS`,
  `UNIT_QUOTE_REFERENCE_PRICE_PER_HOUR`, `UNIT_QUOTE_EGRESS_PER_GB=0`,
  `UNIT_QUOTE_STORAGE_PER_GB_MONTH=0`, learning bounds.
- Disabled means every route returns 404 and nothing is recorded.

## Quote math
GPU-seconds = `expected_units * est_gpu_seconds_per_unit` (the Kit's `billing_hints`), multiplied by the learned
ratio when enough samples exist. Cost = `GPU-seconds / 3600 * price_per_hour`, plus the active platform fee
rate (`get_active_fee_rate`). Price per hour is the chosen machine's asking price, else the reference price.
The result is a low / expected / high range with a stated confidence (`low`, `medium`, `high` by sample count).
Both ends and the expected value are clamped to `max_cost` (required on the request), so a quote can never
show or authorise more than the cap. If the unclamped high exceeds the cap the quote says `capped=true`.

Egress and storage are separate line items, not included in the GPU range. A rate of 0 means "not charged".
Nothing here bills them; billing them needs its own owner-approved change.

## Learned correction
`record_actual()` stores (estimated, actual) GPU-seconds per kit+version+unit. With at least
`UNIT_QUOTE_MIN_SAMPLES` samples the median of actual/estimated is used as a ratio, bounded to
`[UNIT_QUOTE_RATIO_MIN, UNIT_QUOTE_RATIO_MAX]`, and the corrected per-unit estimate is never below the
smallest observed actual per unit. Implausible samples are dropped.

## Not done (needs owner or other code)
- Nothing calls `record_actual()` yet; wiring it at job completion touches settlement and was out of scope.
- Linking at submit is the `/link` route / `link_quote_to_job()`; `jobs_v2` submit is not edited.
- Rates and TTL are placeholders. The quote request is called from `apps/web/src/lib/kits.ts` (`POST /v1/unit-quotes`, absent while the feature is off); no quote display component exists yet (U15.2).

## Review checklist before enabling
1. Independent review of `unit_quotes.py` rounding and cap logic.
2. Owner sets reference price, egress and storage rates.
3. Ledger test for U15.1 (held cap never exceeded) run and passing.
