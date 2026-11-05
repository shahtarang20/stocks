# U16 Acceptable use and safety design (ADR-0006 WP-U16)

Status: code written, NOT run or tested. Date: 2026-11-03.

**LEGAL SIGN-OFF IS REQUIRED BEFORE ENABLING.** The AUP text in the web app is a DRAFT for counsel.
Checkpoint U16.3 (content-safety and takedown process) is a legal deliverable. Until counsel approves,
`AUP_REQUIRED` stays `false` and `ABUSE_ENFORCE` stays `false`.

## Components
1. **AUP acceptance at sign-up (U16.1).** `users.aup_version` and `users.aup_accepted_at` (migration
   0032). `RegisterRequest` gains optional `accept_aup: bool` and `aup_version: str`. When
   `AUP_REQUIRED=true`, registration without `accept_aup=true` and a matching `AUP_CURRENT_VERSION` is
   rejected (422 `aup_not_accepted`). When false (default), behaviour is unchanged; if the client sends
   acceptance anyway it is still recorded. Hosts are users, so one record covers customers and hosts.
2. **Abuse detector (U16.2)** `services/abuse.py`, pure functions plus a DB sweep:
   - sustained high GPU util (>= `ABUSE_UTIL_THRESHOLD_PCT`, 98) for >= `ABUSE_UTIL_WINDOW_SECONDS` (900)
     while no progress advances and step_count is flat;
   - repeated identical non-progress samples (same util, VRAM, step) over N samples;
   - egress attempts (network-policy requests or agent-reported blocked destinations) matching
     known mining pools, stratum ports or miner process names.
   Each hit creates an `AbuseFlag` (one open flag per job and signal kind).
3. **Enforcement mode.** `ABUSE_ENFORCE=false` (default) is observe-only: flags are created, nothing
   is stopped. When true: the job is cancelled via the normal job FSM, `users.status` is NOT changed;
   instead the customer is put on a submission hold (`abuse_holds`: row with `released_at` null) that
   blocks new submissions via `assert_can_submit()` until an admin releases it. `assert_can_submit` is called at
   submit by `POST /v1/jobs`, `POST /v1/jobs/v2`, `POST /v1/jobs/v2/fanout`, pipelines, BYOC, `POST /v1/sessions` and
   `POST /v1/services` (403 `submissions_suspended_pending_review`); it is a no-op unless `ABUSE_ENFORCE=true`.
4. **Admin review** `/v1/admin/abuse/...`: list flags, get, resolve (`confirmed` | `dismissed`) with a
   note, release hold (`POST /v1/admin/abuse/users/{user_id}/release-hold`), list reports. All audited.
5. **Report and takedown** `POST /v1/abuse/report` (authenticated, rate limited by size caps) records
   an `AbuseReport` (job or artifact reference, category, description). Admin can action it
   (`/v1/admin/abuse/reports/{id}/takedown`), which cancels the job if active and marks the report.
   Counsel must define categories, response times and notice format.

## Limits and honesty
- Heuristics produce false positives (a legitimate fully utilised long training run has high util). With
  observe-only default, humans review before anything is enforced. Tune thresholds with real data.
- Process-name signals depend on agent reporting `extra_metrics.process_names`; a hostile host controls
  that. Not a proof, one signal among several.
- No content scanning is implemented. Illegal-content handling is a legal process, not code.
