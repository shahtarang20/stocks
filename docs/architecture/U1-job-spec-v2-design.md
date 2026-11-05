# U1 design note: Job Spec v2 and Kit Registry

Status: draft for owner approval (L8). Implements ADR-0006 section 6, 8 and WP-U1 (checkpoints U1.1-U1.4).
Owner decision still open: **who holds the Kit signing key** (code only verifies; see Security).

## Scope of this package

In: v2 spec schema and validation, Kit model and registry, signed manifests, admin publish, public read
routes, a validate-only route returning field-level errors.
Out (later packages): actually scheduling a v2 job (U7), data plane references (U2), fan-out (U4),
BYOC `image` (U8), pipelines/sessions/services (U9-U11), price units in money code (U15).
v1 jobs: `POST /v1/jobs`, `JobCreate`, scheduler, ledger, pricing are not touched (P10, U1.1).

## Interfaces

Routes (`app/api/routers/kits.py`):

| Route | Auth | Purpose |
|---|---|---|
| `GET /v1/kits?category=` | public | latest non-yanked version of each Kit |
| `GET /v1/kits/{slug}` | public | all versions of a Kit |
| `GET /v1/kits/{slug}/{version}` | public | one version with manifest, signature, key id |
| `POST /v1/kits/validate-spec` | logged-in user | validate a v2 spec; 200 `{valid, kit, effective_resources}` or 422 |
| `POST /v1/admin/kits` | administrator | publish `{manifest, signature}`; 201, or 409 if (slug, version) exists |
| `POST /v1/admin/kits/{slug}/{version}/yank` | administrator | mark unusable for new jobs (only mutable field) |

422 body: `{"detail": {"errors": [{"field": "params.frames", "code": "...", "message": "..."}]}}`.

Services (`app/services/kits.py`): `canonical_manifest_bytes`, `verify_manifest_signature`,
`validate_manifest`, `publish_kit`, `get_kit`, `validate_job_spec_v2`.
Schemas (`app/schemas/job_spec_v2.py`): `JobSpecV2` (pydantic, `extra="forbid"`).

## Job Spec v2 (fields accepted in U1)

`version: 2`, `kit: "slug@version"` (exact version, no ranges), `mode: "batch"` (others refused until built),
`inputs[{name, source}]` where source is `dataset:<id>` or `artifact:<job>/<name>`, `outputs[{name, stream_previews}]`
(names must exist in the Kit; paths come from the Kit, never from the customer), `resources{gpu{count,
min_vram_gb, min_compute_capability}, cpu, ram_gb, disk_gb}` (may raise the Kit's minimums, never lower them),
`params` (validated against the Kit's JSON Schema), `billing{unit, expected_units, max_cost}` (hints only; the unit
must equal the Kit's; billing stays GPU-seconds, P5). Unknown fields anywhere are refused. `image`/`command`
(BYOC), `network`, `parallelism`, `privacy` are refused with `not_supported_yet` rather than ignored (L1).

## Data model (migration `0026_kits`, revises `0025_withdrawals`)

Table `kits`: `id`, `slug`, `version`, `name`, `category`, `image_digest` (`name@sha256:<64 hex>`), `manifest` (JSON),
`manifest_sha256`, `signature` (hex), `signing_key_id` (sha256 prefix of the public key), `status`
(`published` | `yanked`), `published_by`, `created_at`, `yanked_at`. Unique `(slug, version)`.
Immutability: an ORM `before_update` guard raises if any column except `status`/`yanked_at` changes; there is no
update or delete route. A fix is a new version.

Manifest (the signed object): `schema: 1`, `slug`, `version`, `name`, `description`, `category`, `image`,
`mode`, `param_schema` (JSON Schema 2020-12), `inputs[{name, kind, required}]`, `outputs[{name, path}]`,
`resource_defaults{gpu_count, min_vram_gb, min_compute_capability, cpu, ram_gb, disk_gb}`,
`billing_hints{unit, est_gpu_seconds_per_unit}`, `verification{method}`.

## Security

- Signature: Ed25519 over `b"computecoin-kit-v1\n" + canonical JSON` (sorted keys, no spaces, UTF-8). Public key in
  `KIT_SIGNING_PUBLIC_KEY` (64 hex chars). Unset key means publish and use are refused (fail closed, L1).
- Verified at publish AND again on every use in `validate_job_spec_v2` (U1.4): a row edited in the database
  directly fails the signature or hash check and the Kit is refused.
- No private key in the repo (L7). `scripts/sign_kit.py` reads the key from a path given at run time, offline.
- Image must be pinned by digest. Param schema is checked with `check_schema`, must be an object schema with
  `additionalProperties: false`, size-capped (64 KiB), and may contain no `$ref` except local `#/...` (no remote fetches).
- Public read routes expose only manifests and signatures. Publishing is audited.

## Tests (`tests/test_kits_u1.py`, written, not run)

Valid publish and read; unsigned/tampered/wrong-key refused (U1.4); duplicate version 409 and ORM update refused
(U1.3); mutable-image-tag and remote-`$ref` schema refused; v2 spec: unknown field, bad kit ref, bad param,
resource below Kit minimum, unknown input/output, unsupported mode give per-field 422 (U1.2); DB-tampered Kit refused
at use; v1 job creation still works (U1.1, existing suite).
Bug re-insertion list: remove signature check at publish; at use; `extra="forbid"`; immutability guard; `$ref` check.

## Unverified / dependencies

`jsonschema` is added to `pyproject.toml` (not installed by this work, L11). Its API
(`Draft202012Validator`, `check_schema`, `iter_errors`) and `cryptography` Ed25519 calls must be checked against
current docs (L4) before merge.
