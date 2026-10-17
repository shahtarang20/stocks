# Phase 2 — Provider onboarding & machine registration protocol (AUTHORITATIVE CONTRACT)

This document is the single source of truth for the Phase 2 provider/agent
protocol. The backend (`apps/api`), the provider agent (`provider-agent`), and
the web app (`apps/web`) MUST all conform to exactly this. Do not change shapes
unilaterally — if a change is needed, change it here first.

All request/response bodies are JSON. Timestamps are RFC 3339 UTC strings unless
noted as unix seconds. Money is a decimal string (never a float).

## Identity & auth models

- **User access JWT** (from Phase 1, `Authorization: Bearer <access_token>`) —
  authenticates a logged-in user acting as a provider for *management* endpoints.
- **Enrollment token** (`enr_<random>`) — short-lived, single-use, revocable,
  provider-scoped. Used ONCE by the agent to register a machine. Stored hashed
  (SHA-256) server-side; the raw value is shown to the provider exactly once.
- **Machine credential** (`machine_id` + `machine_secret`) — issued to a machine
  at enrollment. `machine_secret` has the form `msk_<signing_key>` and is
  returned exactly once. The agent signs every agent-endpoint request with it
  (HMAC).

  **HMAC key storage (trust decision — resolves an original contradiction).**
  The protocol requires the server to *recompute* `HMAC-SHA256(key, canonical)`
  to verify heartbeats, which is impossible if the key is stored only as a
  one-way SHA-256 hash. Resolution: the HMAC key is the `signing_key` — the
  random component of `machine_secret` after the `msk_` prefix. The server
  persists `signing_key` and treats it as a **secret at rest** (it is the one
  piece of credential material that must remain recoverable in order to verify
  signatures). The raw `machine_secret` string itself is never persisted; for
  audit parity the server additionally stores `secret_hash = SHA-256(machine_secret)`.
  Verification recomputes `HMAC-SHA256(signing_key, canonical)` and compares in
  constant time. Deregistering a machine clears its `signing_key`, so later
  heartbeats fail (401).

## Capability honesty

Capability is always reported as `verification_status: "detected"` (self-measured
by the agent). The agent MUST NEVER emit `"verified"` or `"benchmarked"`. Only a
future platform-side benchmarking step may raise that. The UI must show a
"Detected" badge, not "Verified".

## Telemetry storage rule

Heartbeats update the machine row's `last_heartbeat_at` + `last_health` (JSON)
only. Do NOT create a new relational row per heartbeat (spec §19). High-frequency
telemetry is a future metrics-store concern.

---

## Management endpoints (User JWT)

### POST /provider/register
Make the current user a provider (idempotent). Grants the `provider` role.
```
req:  { "display_name": "string?", "contact_email": "string?" }
res 200: { "provider_id": "uuid", "display_name": "...", "status": "active", "created_at": "..." }
```

### POST /provider/enrollment-tokens
Mint a one-time enrollment token. Raw token shown ONCE.
```
req:  { "label": "string?" }
res 201: {
  "enrollment_token_id": "uuid",
  "enrollment_token": "enr_xxxxxxxx",   // shown once, never again
  "expires_at": "...",                  // default now + 1h
  "label": "..."
}
```

### GET /provider/enrollment-tokens
List own tokens (never returns the raw secret).
```
res 200: [ { "id": "uuid", "label": "...", "expires_at": "...", "used_at": null|"...", "revoked_at": null|"...", "machine_id": null|"uuid", "created_at": "..." } ]
```

### DELETE /provider/enrollment-tokens/{id}
Revoke an unused token. → 204.

### GET /provider/machines
List the provider's own machines. `status` is derived: `online` if
`last_heartbeat_at` is within `ONLINE_WINDOW_SECONDS` (90) and availability is
`online`, else `offline`/`maintenance`.
```
res 200: [ {
  "machine_id": "uuid",
  "hostname": "...",
  "agent_version": "...",
  "status": "online|offline|maintenance",
  "availability": "online|offline|maintenance",
  "verification_status": "detected",
  "asking_price_per_hour": "1.50"|null,
  "max_concurrent_jobs": 1,
  "region": "..."|null,
  "last_heartbeat_at": "..."|null,
  "hardware": {
     "gpu_model": "..."|null, "gpu_count": 0, "vram_mb": 0,
     "cpu_model": "...", "cpu_cores": 0, "ram_mb": 0, "disk_gb": 0, "cuda_version": null
  },
  "supported_workloads": ["text-generation","batch-inference", ...],
  "created_at": "..."
} ]
```

### GET /provider/machines/{id}
Single machine (owner only; 404 if not owned). Same shape as list element + `last_health`.

### PATCH /provider/machines/{id}
Owner updates operational settings.
```
req:  { "availability": "online|offline|maintenance"?, "asking_price_per_hour": "1.50"?, "max_concurrent_jobs": 2?, "region": "us-east"?, "supported_workloads": [...]? }
res 200: <machine object>
```

### DELETE /provider/machines/{id}
Deregister: sets status=deregistered, revokes machine credential (future
heartbeats 401). → 204.

---

## Agent endpoints

### POST /v1/agents/enroll   (auth: enrollment token in body)
```
req: {
  "enrollment_token": "enr_xxxx",
  "agent_version": "0.1.0",
  "hostname": "...",
  "capability_report": {
    "verification_status": "detected",
    "cpu_model": "...", "cpu_cores": 0, "ram_mb": 0, "disk_gb": 0,
    "gpus": [ { "model": "...", "vram_mb": 0, "cuda_version": "..."|null } ],   // [] on CPU-only
    "supported_workloads": ["text-generation","batch-inference"]
  }
}
res 201: {
  "machine_id": "uuid",
  "machine_secret": "msk_xxxx",         // shown once, store hashed
  "heartbeat_interval_seconds": 30,
  "online_window_seconds": 90
}
errors: 400 invalid body; 401 token invalid/expired/used/revoked.
```
Server: validate token → create `provider_machines` (availability=offline,
status=registered) + `hardware_inventory` snapshot (DETECTED) + capabilities;
mark token used and link `machine_id`; issue machine credential.

### POST /v1/agents/heartbeat   (auth: machine HMAC)
Headers:
```
X-ComputeCoin-Machine:    <machine_id>
X-ComputeCoin-Timestamp:  <unix seconds>
X-ComputeCoin-Signature:  <hex HMAC-SHA256(machine_secret, canonical)>
```
Canonical string signed (exact, `\n`-joined, no trailing newline):
```
<machine_id>\n<unix_timestamp>\n<sha256_hex(raw_request_body_bytes)>
```
Body:
```
req: {
  "availability": "online|maintenance",
  "capability_summary": { "gpu_count": 0, "vram_mb": 0 },
  "health": { "gpu_temp_c": null, "gpu_util_pct": null, "mem_used_mb": null, "uptime_s": 0 }
}
res 200: { "ok": true, "directives": {} }
errors: 401 unknown machine / bad signature / deregistered;
        409/401 timestamp outside ±SKEW (300s) → reject (replay protection).
```
Server: look up machine, recompute `HMAC-SHA256(signing_key, canonical)` with
the stored `signing_key` (constant-time compare — see "HMAC key storage" above),
check timestamp skew (±300s), update `last_heartbeat_at` + `last_health` +
`availability`. No new row per heartbeat. `machine_secret` is `msk_<signing_key>`
and the HMAC key is `signing_key` (the substring after `msk_`).

---

## New DB tables (Phase 2)
`providers`, `provider_machines`, `hardware_inventory`, `provider_capabilities`,
`enrollment_tokens`. (No `provider_heartbeats` table — see telemetry rule.)
Portable column types only (Uuid, JSON, Numeric, DateTime(timezone=True)).

## Isolation
A provider can only see/modify their own machines and tokens. Non-owned →
404. Agent endpoints never accept a user JWT; management endpoints never accept
a machine credential.
