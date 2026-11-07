# U2 Data plane: design note

Status: code written, NOT run (no tests, builds or installs were executed). Awaiting owner approval (L8).
Parent: ADR-0006 section 9 and principle P4 (file bytes never pass through the API). Extends, does not replace,
`app/services/storage.py` (artifacts) and `datasets.py` (small HMAC-grant datasets), which are untouched.

## Owner decisions (NOT made here)

These are settings with placeholder defaults, all in `app/core/config.py` under `DATA_PLANE_*`. The whole
feature is OFF (`DATA_PLANE_ENABLED=false`) and refuses requests until configured.

| Decision | Setting(s) | Placeholder |
|---|---|---|
| Storage provider | `DATA_PLANE_ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY_ID/REGION/ADDRESSING_STYLE` | none (any S3-compatible: S3, R2, MinIO, B2) |
| Size limits | `DATA_PLANE_MAX_FILE_BYTES`, `DATA_PLANE_ACCOUNT_QUOTA_BYTES`, `DATA_PLANE_PART_SIZE_BYTES` | 1 TiB, 100 GiB, 64 MiB |
| Retention | `DATA_PLANE_RETENTION_DEFAULT_DAYS`, `_MAX_DAYS`, `DATA_PLANE_GC_GRACE_SECONDS`, `DATA_PLANE_UPLOAD_TTL_SECONDS` | keep forever, no max, 1 day, 7 days |
| Cross-account dedup | `DATA_PLANE_CROSS_ACCOUNT_DEDUP` | off (see Security) |
| Large-file verifier placement | `DATA_PLANE_INLINE_VERIFY_MAX_BYTES` | 256 MiB inline, larger in the worker |
| BYO buckets | `DATA_PLANE_BYO_ENABLED`, `_BYO_MAX_TTL_SECONDS`, `DATA_PLANE_CREDENTIAL_KEY` | off, 3600 s |

## Interfaces

`app/services/data_plane_store.py`: `DataPlaneStore` (ABC) is the only thing that talks to the vendor:
`create_multipart`, `presign_upload_part` (checksum header signed in), `list_parts`, `complete_multipart`,
`abort_multipart`, `head_size`, `sha256_of` (streamed, bounded memory), `copy` (server-side), `delete`,
`presign_download`. `S3DataPlaneStore` is the boto3 implementation; tests inject a fake with
`set_data_store(...)`. `ByoClient` (`presign`, `scoped_credentials`) is the customer-bucket side
(`S3ByoClient`: presigned URL; STS AssumeRole with an inline session policy). No local-disk fallback:
unconfigured means refused (L1).

REST (`app/api/routers/data_plane.py`, prefix `/v1/data`): uploads (create with `Idempotency-Key`, get state,
part URL, confirm part, complete, abort), files (list, download link, delete), usage, BYO buckets
(register, list, revoke, job URL, job credentials). All owner-scoped; other owners get 404.

## Data model (migration `0027_data_plane`, revises `0026_kits`)

- `data_objects`: one blob per `(scope_key, sha256)`, `storage_key = cas/<scope>/<aa>/<sha256>`, `refcount`
  (CHECK >= 0), `status` ready|deleting, `last_referenced_at`.
- `data_files`: owner's named reference (name, type, size, sha256, `expires_at`, `deleted_at`). `object_id`
  is cleared when its object is garbage-collected.
- `data_uploads`: state machine active -> verifying -> complete, or failed / aborted / expired; unique
  `(owner, idempotency_key)`; `staging_key` under `staging/<owner>/<id>`.
- `data_upload_parts`: per part declared SHA-256, size, etag, status pending|verified|corrupt, attempts.
- `byo_buckets`: endpoint, bucket, region, forced non-empty `prefix`, mode presign|sts, `role_arn`,
  `credentials_enc` (AES-GCM, key derived from `DATA_PLANE_CREDENTIAL_KEY`), `revoked`.

## Flows

1. **Create.** Validate size/quota/name/hash. Same `(scope, sha256, size)` already stored: take a reference
   atomically, create the file, return it with no upload (U2.3). Otherwise `create_multipart(staging)`.
   Part size is raised so the part count stays under 10 000 (S3 limit); minimum 5 MiB.
2. **Part.** Client states the part's SHA-256 and size; API returns a presigned PUT (TTL <= 900 s, never past
   the upload's expiry) whose checksum header is signed, so the store rejects a body that differs. The client
   sends bytes straight to the store.
3. **Confirm.** API reads `ListParts` and compares the checksum and size the store recorded with the declared
   ones. Mismatch or no checksum: part marked `corrupt`, HTTP 409 `{"action":"resend"}`; the client asks for a
   new URL for the same part number and sends only that part again (U2.2). Verified parts are never resent;
   `GET /uploads/{id}` lists verified/corrupt/pending/missing parts, so a dropped connection resumes (U2.1).
4. **Complete.** All parts present and verified, sizes add up, and every part re-checked against the store
   (a part overwritten after confirmation is refused). Then `complete_multipart`, status `verifying`.
5. **Whole-file proof.** `sha256_of(staging)` must equal the declared hash and the size must match. Inline
   when small, else the worker does it (resumable: a store outage leaves `verifying` and is retried).
   Mismatch is final: staging deleted, nothing becomes addressable. Success: `copy` to the content-addressed
   key, insert/reference the object (unique constraint resolves races), create the file, delete staging.
6. **Download.** Presigned GET for exactly one object, TTL clamped to `DATA_PLANE_DOWNLOAD_MAX_TTL_SECONDS`,
   attachment disposition and octet-stream type (no browser rendering). Expired or deleted files are 404
   immediately, before the sweeper runs. Audit entry per link.
7. **BYO bucket.** Prefix is mandatory; ComputeCoin may only touch `<prefix>jobs/<job_id>/`. `sts` mode issues
   temporary credentials with a session policy limited to that prefix (read-only or read/write) and expiry
   (capped by `DATA_PLANE_BYO_MAX_TTL_SECONDS`); `presign` mode (R2, B2, MinIO: no STS) issues one presigned
   URL per key under the prefix. The caller must own the job and the bucket. Revoke wipes the secret.
8. **Sweeper** (`sweep_data_plane`, worker step with its own try/except and timer): abort expired uploads,
   finish `verifying` ones, drop files past `expires_at` (decrement once, atomically), claim and delete
   unreferenced objects after the grace period (claim = atomic status flip, so a racing dedup hit uploads
   normally), retry objects stuck in `deleting`.

## Security

- P4: no endpoint accepts or returns file bytes. The only byte reader is the verifier (`sha256_of`), streaming.
- Dedup oracle: confirming "this hash exists" lets an attacker who knows a hash claim someone else's file.
  Default is per-account dedup (scope key = owner); cross-account dedup is an owner decision and, if enabled,
  should require a proof-of-possession step (implemented, off unless cross-account dedup is enabled; never run against real storage) and stay off for privacy tiers.
- Presigned URLs are bearer capabilities: short TTL, one key, one part, never logged by us.
- Staged bytes are never addressable until the whole-file hash is proven.
- BYO credentials: encrypted at rest, write-only (never returned), wiped on revoke, key required to enable.
  Endpoints must be https and not loopback/private/link-local literals; a DNS name resolving to a private
  address is NOT caught (residual risk: the API never connects to a customer endpoint, it only signs).
- Privacy tiers (ADR-0005): crypto-shredding of private-tier data (WP24) is implemented for private-tier deletion (never run against real
  storage); for other data, deleting a file only drops a reference and the object after the grace period.
- Names are validated (no control chars or separators); keys are opaque (`..` and leading `/` rejected).

## Tests (`apps/api/tests/test_u2_data_plane.py`, written, not run)

Fake store follows documented S3 behaviour (rejects a checksum mismatch at PUT, ListParts reports the
checksum of what arrived). Covers U2.1 resume, U2.2 corrupt part and re-send, hash mismatch refused, U2.3
store-once and refcount, no cross-account dedup, delete + GC, retention expiry, stale-upload abort, quota and
size limits, idempotency, download scope/TTL/owner, fail-closed when off, HTTP flow, U2.4 BYO scope and expiry,
bad prefix/endpoint, worker step isolation. Still to do: a run against real MinIO and a real S3-compatible
provider; a 50 GB resume (U2.1 as written is a 3-part simulation), the L5 bug re-insertion run.

## Unverified (L4)

boto3/STS/ListParts checksum behaviour and the doc URLs listed in `data_plane_store.py` were not fetched.
R2 compatibility (ChecksumAlgorithm on multipart, ListParts checksum) must be checked before choosing it.
