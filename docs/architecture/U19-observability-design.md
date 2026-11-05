# U19 Observability design (ADR-0006 WP-U19)

Status: code written, NOT run or tested. Date: 2026-11-03.

## Goals (checkpoints)
- U19.1 Per-job GPU utilisation, VRAM, progress % and cost so far visible live.
- U19.2 Platform dashboards and alerts for queue depth, job failures, host health.

## Decisions
1. **No new table, no migration.** Live job data rides the existing `job_telemetry` table. Progress
   goes in `extra_metrics` (`progress_pct`, `phase`, `vram_total_mb`). Additive, so v1 agents and
   old rows are unaffected. (Migration 0031 is therefore used by U16, not U19.)
2. **Progress path.** Agent -> `POST /v1/telemetry/job/progress` (machine HMAC, same scheme as
   telemetry). Accepted only for jobs assigned to the calling machine (same rule as
   `record_job_telemetry`); everything else is dropped silently-counted. Caps: at most
   `JOB_PROGRESS_MAX_POINTS` (100) points per request, `phase` limited to 64 chars, progress clamped
   to 0..100, per-job write rate limited to one point per `JOB_PROGRESS_MIN_INTERVAL_SECONDS` (2).
3. **Customer read path.**
   - `GET /v1/jobs/{id}/live` (polling; works everywhere) returns the latest sample.
   - `GET /v1/jobs/{id}/live/stream` (SSE) emits the same payload every
     `JOB_LIVE_STREAM_INTERVAL_SECONDS` (3) for at most `JOB_LIVE_STREAM_MAX_SECONDS` (300), then closes;
     the client reconnects. Auth is the normal session cookie/bearer; ownership is enforced, others get 404.
4. **Cost so far is an estimate, not billing.** It is `max_cost * elapsed / max_runtime_seconds`,
   capped at `max_cost`, and labelled `cost_so_far_is_estimate: true`. It never touches the ledger,
   pricing or holds. The authoritative charge stays `final_cost`.
5. **Prometheus `/v1/ops/metrics`** (new, separate from the legacy `/metrics`). Disabled (404) unless
   `OBSERVABILITY_METRICS_TOKEN` is set; then `Authorization: Bearer <token>` is required (constant-time
   compare). Metrics: jobs by state, queue depth, machines by availability (online, offline, throttled,
   ...), claim latency (avg and max seconds from creation to start, jobs started in the last
   `OBS_CLAIM_LATENCY_WINDOW_SECONDS`), failed jobs in window, seconds since last OK stake sync and
   chain reconciliation, open abuse flags, active telemetry alerts. No PII, no secrets, bounded label sets.
6. **Dashboard tool (OWNER DECISION).** Files are written for Prometheus + Grafana
   (`infrastructure/observability/`). If the owner prefers a hosted tool (Datadog, Grafana Cloud) the
   metric names are unchanged and the alert rules port directly.

## Not done / assumptions
- "Part failures" is approximated by failed jobs in window; per-part metrics arrive with fan-out (U7/U8).
- Claim latency is derived from `jobs.started_at - jobs.created_at`, not a dedicated claim timestamp.
- The web panel for live data is `apps/web/src/components/jobs/live-panel.tsx`; the endpoints are the contract.
- Nothing was run or tested.
